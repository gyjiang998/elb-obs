'use client'

import { forwardRef, memo, useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react'
import { LoaderCircle, SendHorizontal } from 'lucide-react'
import type { Terminal as XtermTerminal } from '@xterm/xterm'
import type { FitAddon as XtermFitAddon } from '@xterm/addon-fit'
import { getAnalysisAgentSession, type BackendAgentRun } from '../lib/backend-agent-runtime'
import type { BrowserModelConfig } from '../lib/model-config'
import { ApiError } from '../lib/api'
import {
  createAgentCliSession,
  decodeTerminalChunk,
  getAgentCliSession,
  getAgentCliSessionUsage,
  pollAgentCliOutput,
  redrawAgentCliSession,
  resizeAgentCliSession,
  sendAgentCliCommand,
  stopAgentCliSession,
  updateAgentCliContext,
  writeAgentCliInput,
  type AgentCliTerminal,
  type AgentCliOutputChunk,
  type AgentCliTokenUsage,
} from '../lib/kimi-cli-terminal'
import { randomUuid } from '../lib/random-id'
import { createAgentSubmissionGuard, enqueueAgentSubmission, runGuardedAgentSubmission } from '../lib/agent-task-submission'

type StoredTerminalSession = {
  schemaVersion: 'agent_cli_webshell_session/v4'
  adapterId: string
  terminalSessionId: string
  runId: string
  pageSessionId: string
  savedAt: string
}

type StoredPageRunBinding = {
  schemaVersion: 'agent_cli_page_run/v1'
  pageSessionId: string
  runId: string
  savedAt: string
}

export type AgentWebShellHandle = {
  sendCommand: (command: string, options?: { commandId?: string; analysisId?: string; goal?: string; mode?: 'page_agent' | 'device_validation'; context?: Record<string, unknown>; assertCurrent?: () => void }) => Promise<void>
  interrupt: () => Promise<void>
  focus: () => void
  redraw: () => Promise<void>
  restart: () => Promise<void>
  restoreRun: (runId: string, analysisId: string) => Promise<void>
}

export type AgentWebShellState = 'connecting' | 'ready' | 'working' | 'exited' | 'error'

export type AgentWebShellTransportState = 'initializing' | 'online' | 'recovering' | 'offline' | 'restoring' | 'closed'

export type AgentWebShellTransportStatus = {
  state: AgentWebShellTransportState
  detail: string
  lastConnectedAt: string | null
  disconnectedAt: string | null
  retryAttempt: number
}

export type AgentWebShellCommandSubmission = {
  commandId: string
  submittedAt: string
}

type AgentContextSyncOptions = {
  analysisId?: string
  goal?: string
  mode?: 'page_agent' | 'device_validation'
  context?: Record<string, unknown>
  assertCurrent?: () => void
}

const pageSessionIdStorageKey = 'heterogeneous-enable-page-session-id-v1'
const terminalSessionStorageKey = 'heterogeneous-enable-agent-cli-webshell-v4'
const pageRunBindingStorageKey = 'heterogeneous-enable-agent-cli-page-run-v1'
const ptyOfflineGraceMs = 30_000
const activeOutputPollWaitMs = 1_000
const hiddenOutputPollWaitMs = 5_000

function ptyTransportFailureState(disconnectedAt: string): AgentWebShellTransportState {
  const startedAt = Date.parse(disconnectedAt)
  return Number.isFinite(startedAt) && Date.now() - startedAt >= ptyOfflineGraceMs ? 'offline' : 'recovering'
}

function normalizedAnalysisId(value?: string | null): string | null {
  const normalized = value?.trim()
  return normalized || null
}

function pageSessionId(): string {
  const existing = window.sessionStorage.getItem(pageSessionIdStorageKey)?.trim()
  if (existing) return existing
  const created = `page_${randomUuid()}`
  window.sessionStorage.setItem(pageSessionIdStorageKey, created)
  return created
}

function readStoredSession(expectedPageSessionId: string, expectedAdapterId: string): StoredTerminalSession | null {
  try {
    const parsed = JSON.parse(window.sessionStorage.getItem(terminalSessionStorageKey) || '{}') as Partial<StoredTerminalSession>
    if (parsed.schemaVersion !== 'agent_cli_webshell_session/v4' || !parsed.runId || !parsed.terminalSessionId) return null
    if (parsed.pageSessionId !== expectedPageSessionId) return null
    if (parsed.adapterId !== expectedAdapterId) return null
    return parsed as StoredTerminalSession
  } catch {
    return null
  }
}

function readStoredPageRunBinding(expectedPageSessionId: string): StoredPageRunBinding | null {
  try {
    const parsed = JSON.parse(window.sessionStorage.getItem(pageRunBindingStorageKey) || '{}') as Partial<StoredPageRunBinding>
    if (parsed.schemaVersion === 'agent_cli_page_run/v1' && parsed.pageSessionId === expectedPageSessionId && parsed.runId) {
      return parsed as StoredPageRunBinding
    }
    return null
  } catch {
    return null
  }
}

function clearStoredPageRunBinding(): void {
  window.sessionStorage.removeItem(pageRunBindingStorageKey)
  window.sessionStorage.removeItem(terminalSessionStorageKey)
}

function storeSession(terminal: AgentCliTerminal, currentPageSessionId: string): void {
  window.sessionStorage.setItem(terminalSessionStorageKey, JSON.stringify({
    schemaVersion: 'agent_cli_webshell_session/v4',
    adapterId: terminal.adapter_id,
    terminalSessionId: terminal.terminal_session_id,
    runId: terminal.run_id,
    pageSessionId: currentPageSessionId,
    savedAt: new Date().toISOString(),
  } satisfies StoredTerminalSession))
  window.sessionStorage.setItem(pageRunBindingStorageKey, JSON.stringify({
    schemaVersion: 'agent_cli_page_run/v1',
    pageSessionId: currentPageSessionId,
    runId: terminal.run_id,
    savedAt: new Date().toISOString(),
  } satisfies StoredPageRunBinding))
}

function contextSyncInput(
  currentPageSessionId: string,
  analysisId: string | undefined,
  runtimeContext: Record<string, unknown>,
  options?: AgentContextSyncOptions,
) {
  return {
    goal: options?.goal ?? '根据用户目标操作鸿蒙异构优化工作台，并让页面状态与真实工具结果保持一致。',
    pageSessionId: currentPageSessionId,
    mode: options?.mode ?? 'page_agent' as const,
    context: options?.context ?? runtimeContext,
    analysisId: options?.analysisId ?? analysisId,
  }
}

function contextSyncKey(sessionId: string, input: ReturnType<typeof contextSyncInput>): string {
  return JSON.stringify({ sessionId, ...input })
}

const terminalOscPattern = new RegExp(String.raw`\u001b\][^\u0007]*(?:\u0007|\u001b\\)`, 'g')
const terminalCsiPattern = new RegExp(String.raw`\u001b\[[0-?]*[ -/]*[@-~]`, 'g')
const terminalControlPattern = new RegExp(String.raw`[\r\u0000-\u0008\u000b\u000c\u000e-\u001f]`, 'g')

function terminalDetail(raw: string): string {
  const plain = raw
    .replace(terminalOscPattern, '')
    .replace(terminalCsiPattern, '')
    .replace(terminalControlPattern, '')
  const lines = plain
    .split('\n')
    .map((line) => line.trim().replace(/^[│┃┊┋╎╏▏▕]+\s*/, '').trim())
    .filter((line) => {
      if (!line || /^context:\s*\d+%/i.test(line)) return false
      if (/^[\s.\u2026·•┌┐└┘├┤┬┴┼─━═│┃╭╮╰╯]+$/u.test(line)) return false
      if (/^\d+\.\s+[A-Za-z0-9_.:/-]+(?:\s+\([^)]*\))?$/.test(line)) return false
      if (/^[{}[\],:'"`]+$/.test(line)) return false
      if (/^(session|model|version|mcp|workspace|directory|context)\s*:/i.test(line)) return false
      if (/^(auto|plan|ask)\s+\S+\s+.*kimi-engine\/workspaces\//i.test(line)) return false
      return /[\u3400-\u9fff]/u.test(line) || line.length >= 18
    })
  return lines.at(-1)?.slice(0, 240) || ''
}

function isPlainPromptText(value: string): boolean {
  for (const character of value) {
    const code = character.charCodeAt(0)
    if (code === 0x7f || (code < 0x20 && character !== '\n' && character !== '\t')) return false
  }
  return true
}

const AgentLocalComposer = memo(function AgentLocalComposer({
  adapterDisplayName,
  ready,
  onSubmit,
}: {
  adapterDisplayName: string
  ready: boolean
  onSubmit: (command: string) => Promise<void>
}) {
  const [value, setValue] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [cursorIndex, setCursorIndex] = useState(0)
  const [inputScrollTop, setInputScrollTop] = useState(0)
  const [inputViewport, setInputViewport] = useState({ width: 0, height: 0 })
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)

  const syncInputGeometry = useCallback(() => {
    const textarea = textareaRef.current
    if (!textarea) return
    setInputViewport((current) => {
      const next = { width: textarea.clientWidth, height: textarea.clientHeight }
      return current.width === next.width && current.height === next.height ? current : next
    })
  }, [])

  useEffect(() => {
    const textarea = textareaRef.current
    if (!textarea) return
    syncInputGeometry()
    const observer = new ResizeObserver(syncInputGeometry)
    observer.observe(textarea)
    return () => observer.disconnect()
  }, [syncInputGeometry])

  const syncCursor = useCallback((textarea: HTMLTextAreaElement) => {
    setCursorIndex(textarea.selectionEnd)
    setInputScrollTop(textarea.scrollTop)
  }, [])

  const submit = useCallback(async () => {
    const command = value.trim()
    if (!command || submitting || !ready) return
    setSubmitting(true)
    try {
      await onSubmit(command)
      setValue('')
      setCursorIndex(0)
      setInputScrollTop(0)
      if (textareaRef.current) textareaRef.current.scrollTop = 0
      textareaRef.current?.focus()
    } catch {
      textareaRef.current?.focus()
    } finally {
      setSubmitting(false)
    }
  }, [onSubmit, ready, submitting, value])

  return (
    <div className="agent-local-composer" aria-label={`${adapterDisplayName} 消息输入`}>
      <div className="agent-local-composer-input">
        <span className="agent-local-composer-prompt" aria-hidden="true">
          <span>›</span>
        </span>
        <div
          className="agent-local-composer-cursor-viewport"
          aria-hidden="true"
          style={{ width: inputViewport.width, height: inputViewport.height }}
        >
          <div
            className="agent-local-composer-cursor-mirror"
            style={{ transform: `translateY(${-inputScrollTop}px)` }}
          >
            <span>{value.slice(0, Math.min(cursorIndex, value.length))}</span>
            <span className="agent-local-composer-cursor" />
            <span>{value.slice(Math.min(cursorIndex, value.length))}</span>
          </div>
        </div>
        <textarea
          ref={textareaRef}
          value={value}
          rows={4}
          spellCheck={false}
          aria-label={`向 ${adapterDisplayName} 输入消息`}
          placeholder=" 向页面助手发送消息"
          onChange={(event) => {
            setValue(event.target.value)
            syncCursor(event.target)
          }}
          onScroll={(event) => setInputScrollTop(event.currentTarget.scrollTop)}
          onSelect={(event) => syncCursor(event.currentTarget)}
          onKeyDown={(event) => {
            if (event.key !== 'Enter' || event.shiftKey || event.nativeEvent.isComposing) return
            event.preventDefault()
            void submit()
          }}
        />
      </div>
      <button
        className="agent-local-composer-send"
        type="button"
        title="发送消息"
        aria-label={`向 ${adapterDisplayName} 发送消息`}
        disabled={!ready || submitting || !value.trim()}
        onClick={() => void submit()}
      >
        {submitting ? <LoaderCircle size={15} className="spin" /> : <SendHorizontal size={15} />}
      </button>
    </div>
  )
})

export const AgentWebShell = forwardRef<AgentWebShellHandle, {
  connected: boolean
  adapterId: string
  adapterDisplayName: string
  waitingDetail?: string
  config: BrowserModelConfig
  analysisId?: string
  runtimeContext: Record<string, unknown>
  onSessionReady: (terminal: AgentCliTerminal, run: BackendAgentRun) => void
  onStateChange: (state: AgentWebShellState, detail: string) => void
  onTransportChange: (status: AgentWebShellTransportStatus) => void
  onTokenUsage: (usage: AgentCliTokenUsage) => void
  onCommandSubmitted: (submission: AgentWebShellCommandSubmission) => void
}>(function AgentWebShell({ connected, adapterId, adapterDisplayName, waitingDetail, config, analysisId, runtimeContext, onSessionReady, onStateChange, onTransportChange, onTokenUsage, onCommandSubmitted }, ref) {
  const mountRef = useRef<HTMLDivElement | null>(null)
  const terminalRef = useRef<XtermTerminal | null>(null)
  const fitAddonRef = useRef<XtermFitAddon | null>(null)
  const sessionRef = useRef<AgentCliTerminal | null>(null)
  const outputPollControllerRef = useRef<AbortController | null>(null)
  const resizeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const inputTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const idleTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const usagePollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const lastBackendSizeRef = useRef('')
  const inputBufferRef = useRef<string[]>([])
  const inputChainRef = useRef(Promise.resolve())
  const outputTailRef = useRef('')
  const lastProgressEmissionRef = useRef(0)
  const latestSequenceRef = useRef(0)
  const configRef = useRef(config)
  const contextRef = useRef(runtimeContext)
  const analysisIdRef = useRef(analysisId)
  const pageSessionIdRef = useRef<string | null>(null)
  const connectPromiseRef = useRef<Promise<void> | null>(null)
  const contextSyncChainRef = useRef(Promise.resolve())
  const lastContextSyncKeyRef = useRef('')
  const reconnectSessionRef = useRef<((forceRestart?: boolean) => Promise<void>) | null>(null)
  const requestedRunRestoreRef = useRef<{ runId: string; analysisId: string } | null>(null)
  const scheduleRecoveryRef = useRef<(() => void) | null>(null)
  const recoveryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const recoveryAttemptRef = useRef(0)
  const recoveryActiveRef = useRef(false)
  const disposedRef = useRef(false)
  const submissionTargetRef = useRef({ generation: 0, adapterId, disposed: false })
  if (submissionTargetRef.current.adapterId !== adapterId) {
    submissionTargetRef.current = { ...submissionTargetRef.current, adapterId, generation: submissionTargetRef.current.generation + 1 }
  }
  useEffect(() => {
    submissionTargetRef.current.disposed = false
    return () => {
      submissionTargetRef.current.disposed = true
      submissionTargetRef.current.generation++
    }
  }, [])
  const connectedRef = useRef(connected)
  const transportStatusRef = useRef<AgentWebShellTransportStatus | null>(null)
  const lastTransportSuccessAtRef = useRef<string | null>(null)
  const transportDisconnectedAtRef = useRef<string | null>(null)
  const [ready, setReady] = useState(false)

  configRef.current = config
  contextRef.current = runtimeContext
  analysisIdRef.current = analysisId
  connectedRef.current = connected

  const emitTransportStatus = useCallback((
    state: AgentWebShellTransportState,
    detail: string,
    retryAttempt = 0,
  ) => {
    const now = new Date().toISOString()
    if (state === 'online') {
      lastTransportSuccessAtRef.current = now
      transportDisconnectedAtRef.current = null
    } else if (!transportDisconnectedAtRef.current && ['recovering', 'offline', 'restoring'].includes(state)) {
      transportDisconnectedAtRef.current = now
    }
    const status: AgentWebShellTransportStatus = {
      state,
      detail,
      lastConnectedAt: lastTransportSuccessAtRef.current,
      disconnectedAt: transportDisconnectedAtRef.current,
      retryAttempt,
    }
    transportStatusRef.current = status
    onTransportChange(status)
  }, [onTransportChange])

  const syncContext = useCallback(async (sessionId: string, options?: AgentContextSyncOptions) => {
    const currentPageSessionId = pageSessionIdRef.current ?? pageSessionId()
    pageSessionIdRef.current = currentPageSessionId
    const input = contextSyncInput(currentPageSessionId, analysisIdRef.current, contextRef.current, options)
    const key = contextSyncKey(sessionId, input)
    const operation = contextSyncChainRef.current.then(async () => {
      options?.assertCurrent?.()
      if (lastContextSyncKeyRef.current === key) return null
      const response = await updateAgentCliContext(sessionId, input)
      if (sessionRef.current?.terminal_session_id !== sessionId) return response
      lastContextSyncKeyRef.current = key
      onSessionReady(response.terminal, response.run)
      onTokenUsage(response.terminal.token_usage)
      storeSession(response.terminal, currentPageSessionId)
      return response
    })
    contextSyncChainRef.current = operation.then(() => undefined, () => undefined)
    return operation
  }, [onSessionReady, onTokenUsage])

  const startUsagePolling = useCallback((terminal: AgentCliTerminal, currentPageSessionId: string) => {
    if (usagePollTimerRef.current) clearInterval(usagePollTimerRef.current)
    const refresh = async () => {
      try {
        const usage = await getAgentCliSessionUsage(terminal.terminal_session_id, currentPageSessionId)
        if (sessionRef.current?.terminal_session_id === terminal.terminal_session_id) onTokenUsage(usage)
      } catch {
        // PTY reconnect and backend restart are handled by the terminal stream.
      }
    }
    void refresh()
    usagePollTimerRef.current = setInterval(() => void refresh(), 2500)
  }, [onTokenUsage])

  const flushInput = useCallback(() => {
    const raw = inputBufferRef.current.join('')
    inputBufferRef.current = []
    if (!raw) return
    const session = sessionRef.current
    if (!session) return
    // Preserve xterm's raw CR for Enter. Kimi's interactive TUI distinguishes
    // it from LF and submits the prompt only on the terminal carriage return.
    const includesSubmit = raw.includes('\r') || raw.includes('\n')
    const submission = includesSubmit ? {
      commandId: `terminal-input:${randomUuid()}`,
      submittedAt: new Date().toISOString(),
    } : null
    if (submission) onCommandSubmitted(submission)
    inputChainRef.current = inputChainRef.current
      .then(async () => {
        if (includesSubmit) {
          onStateChange('working', `命令已提交，${session.agent_display_name} 正在处理。`)
          await syncContext(session.terminal_session_id)
        }
        const submitKey = raw.endsWith('\r') || raw.endsWith('\n') ? raw.slice(-1) : ''
        const content = submitKey ? raw.slice(0, -1) : raw
        // A fast browser paste can collapse text and Enter into one xterm
        // callback. Submit that plain-text batch through the PTY's bracketed
        // paste command path; navigation/control sequences stay byte-accurate.
        if (submitKey && content && isPlainPromptText(content)) {
          await sendAgentCliCommand(session.terminal_session_id, content, submission?.commandId ?? `terminal-input:${randomUuid()}`)
          return
        }
        if (content) {
          await writeAgentCliInput(session.terminal_session_id, new TextEncoder().encode(content))
        }
        if (submitKey) await writeAgentCliInput(session.terminal_session_id, new TextEncoder().encode(submitKey))
      })
      .catch((error) => onStateChange('error', error instanceof Error ? error.message : `写入 ${adapterDisplayName} 失败。`))
  }, [adapterDisplayName, onCommandSubmitted, onStateChange, syncContext])

  const queueInput = useCallback((data: string) => {
    inputBufferRef.current.push(data)
    if (inputTimerRef.current) clearTimeout(inputTimerRef.current)
    inputTimerRef.current = setTimeout(flushInput, data.includes('\r') || data.includes('\n') ? 0 : 48)
  }, [flushInput])

  const consumeOutputChunks = useCallback(async (payloads: AgentCliOutputChunk[]) => {
    const terminal = terminalRef.current
    if (!terminal) return
    const pending = payloads.filter((payload) => payload.sequence > latestSequenceRef.current)
    if (pending.length === 0) return
    const decodedChunks = pending.map((payload) => decodeTerminalChunk(payload.data_base64))
    const byteCount = decodedChunks.reduce((total, bytes) => total + bytes.byteLength, 0)
    const bytes = new Uint8Array(byteCount)
    let offset = 0
    for (const chunk of decodedChunks) {
      bytes.set(chunk, offset)
      offset += chunk.byteLength
    }
    await new Promise<void>((resolve) => terminal.write(bytes, resolve))
    if (disposedRef.current || terminalRef.current !== terminal) return
    latestSequenceRef.current = pending.at(-1)?.sequence ?? latestSequenceRef.current
    terminal.refresh(0, Math.max(0, terminal.rows - 1))
    terminal.scrollToBottom()
    const decoded = new TextDecoder('utf-8', { fatal: false }).decode(bytes)
    outputTailRef.current = `${outputTailRef.current}${decoded}`.slice(-8_000)
    const detail = terminalDetail(outputTailRef.current)
    const now = Date.now()
    if (detail && now - lastProgressEmissionRef.current >= 250) {
      lastProgressEmissionRef.current = now
      onStateChange('working', detail)
    }
    if (idleTimerRef.current) clearTimeout(idleTimerRef.current)
    idleTimerRef.current = setTimeout(() => {
      if (!disposedRef.current && sessionRef.current?.state === 'running') {
        onStateChange('ready', `${sessionRef.current?.agent_display_name ?? adapterDisplayName} 已连接，等待输入或页面任务。`)
      }
    }, 3000)
  }, [adapterDisplayName, onStateChange])

  const scheduleRecovery = useCallback(() => {
    if (!recoveryActiveRef.current || disposedRef.current || recoveryTimerRef.current) return
    const attempt = recoveryAttemptRef.current
    const delay = Math.min(5_000, attempt === 0 ? 250 : 500 * (2 ** Math.min(attempt - 1, 4)))
    recoveryTimerRef.current = setTimeout(() => {
      recoveryTimerRef.current = null
      if (!recoveryActiveRef.current || disposedRef.current) return
      recoveryAttemptRef.current += 1
      void reconnectSessionRef.current?.(false)
    }, delay)
  }, [])

  scheduleRecoveryRef.current = scheduleRecovery

  const openOutputStream = useCallback((terminal: AgentCliTerminal) => {
    outputPollControllerRef.current?.abort()
    const controller = new AbortController()
    outputPollControllerRef.current = controller
    const sessionId = terminal.terminal_session_id
    transportDisconnectedAtRef.current = null
    emitTransportStatus('online', `PTY 链路在线；后端 Agent Session 无空闲超时，进程 PID ${terminal.pid ?? 'unknown'}。`)
    onStateChange('ready', `真实 PTY 已连接，后端进程 PID ${terminal.pid ?? 'unknown'}。`)

    void (async () => {
      let consecutiveFailures = 0
      while (
        !controller.signal.aborted
        && !disposedRef.current
        && sessionRef.current?.terminal_session_id === sessionId
      ) {
        try {
          const response = await pollAgentCliOutput(
            sessionId,
            latestSequenceRef.current,
            controller.signal,
            document.visibilityState === 'hidden' ? hiddenOutputPollWaitMs : activeOutputPollWaitMs,
          )
          if (controller.signal.aborted || outputPollControllerRef.current !== controller) return
          if (consecutiveFailures > 0 || transportStatusRef.current?.state !== 'online') {
            emitTransportStatus('online', `PTY 链路已恢复；已从输出序号 ${latestSequenceRef.current} 连续接收，后端 Agent Session 未重启。`)
          } else {
            lastTransportSuccessAtRef.current = new Date().toISOString()
          }
          consecutiveFailures = 0
          if (response.truncated) {
            terminalRef.current?.writeln('\r\n\x1b[33m[终端历史缓冲已截断，以下继续显示可恢复输出]\x1b[0m')
          }
          await consumeOutputChunks(response.chunks)
          if (response.terminal.state === 'exited') {
            if (sessionRef.current?.terminal_session_id === sessionId) sessionRef.current = response.terminal
            emitTransportStatus('closed', `${response.terminal.agent_display_name} 后端进程已退出，输出链路已经关闭。`)
            onStateChange('exited', `${response.terminal.agent_display_name} 已退出${response.terminal.exit_code === null ? '' : `，退出码 ${response.terminal.exit_code}`}。`)
            return
          }
          if (!response.more_available) {
            const interval = response.chunks.length > 0
              ? 16
              : document.visibilityState === 'hidden' ? 1000 : 100
            await new Promise((resolve) => window.setTimeout(resolve, interval))
          }
        } catch (error) {
          if (controller.signal.aborted || outputPollControllerRef.current !== controller) return
          if (error instanceof ApiError && ['AGENT_CLI_SESSION_NOT_FOUND', 'KIMI_CLI_SESSION_NOT_FOUND'].includes(error.code)) {
            controller.abort()
            if (outputPollControllerRef.current === controller) outputPollControllerRef.current = null
            sessionRef.current = null
            setReady(false)
            recoveryActiveRef.current = true
            recoveryAttemptRef.current = 0
            emitTransportStatus('restoring', `API 已重启，原 PTY 进程不可恢复；正在沿用当前页面的持久 Run 和 Agent 原生 Session 重建终端。`)
            onStateChange('connecting', `后端已重启，正在为当前页面恢复 ${adapterDisplayName} Session。`)
            scheduleRecoveryRef.current?.()
            return
          }
          consecutiveFailures += 1
          const disconnectedAt = transportDisconnectedAtRef.current ?? new Date().toISOString()
          transportDisconnectedAtRef.current = disconnectedAt
          const transportState = ptyTransportFailureState(disconnectedAt)
          emitTransportStatus(
            transportState,
            transportState === 'offline'
              ? `浏览器已连续 30 秒无法读取 PTY 输出，正在从序号 ${latestSequenceRef.current} 恢复（第 ${consecutiveFailures} 次）。后端 ${adapterDisplayName} 进程状态尚待链路恢复后确认；空闲本身不会结束 Session。`
              : `浏览器到 PTY 的输出链路短暂中断，正在从序号 ${latestSequenceRef.current} 恢复（第 ${consecutiveFailures} 次）；当前仍按链路恢复中处理，不判定后端 ${adapterDisplayName} 已退出。`,
            consecutiveFailures,
          )
          await new Promise((resolve) => window.setTimeout(resolve, Math.min(3000, 500 * consecutiveFailures)))
        }
      }
    })()
  }, [adapterDisplayName, consumeOutputChunks, emitTransportStatus, onStateChange])

  const connect = useCallback(async (forceRestart = false) => {
    const terminal = terminalRef.current
    if (!terminal || !connectedRef.current) return
    const currentPageSessionId = pageSessionIdRef.current ?? pageSessionId()
    pageSessionIdRef.current = currentPageSessionId
    if (connectPromiseRef.current) {
      await connectPromiseRef.current
      const currentPoll = outputPollControllerRef.current
      if (!forceRestart && sessionRef.current && currentPoll && !currentPoll.signal.aborted) return
    }
    const operation = (async () => {
      emitTransportStatus(
        forceRestart ? 'restoring' : recoveryActiveRef.current ? 'restoring' : 'initializing',
        forceRestart
          ? `正在显式重启 ${adapterDisplayName} PTY；当前页面的持久 Run 将继续保留。`
          : recoveryActiveRef.current
            ? `正在恢复 ${adapterDisplayName} PTY；当前页面的持久 Run 与 Agent 原生 Session 将优先复用。`
            : `正在连接当前页面的长期 ${adapterDisplayName} PTY Session。`,
        recoveryAttemptRef.current,
      )
      onStateChange('connecting', forceRestart ? `正在重启当前页面的 ${adapterDisplayName}。` : `正在读取当前页面保存的 ${adapterDisplayName} Session。`)
      try {
        const current = sessionRef.current
        const stored = readStoredSession(currentPageSessionId, adapterId)
        const storedRun = readStoredPageRunBinding(currentPageSessionId)
        const adapterChanged = Boolean(current && current.adapter_id !== adapterId)
        // A v4 terminal record may point at an empty replacement Run created
        // by the former restart bug. Trust it only when its PTY still exists;
        // otherwise let the backend reconcile the page's durable Run history.
        let requestedRestore = requestedRunRestoreRef.current
        const routeAnalysisId = normalizedAnalysisId(analysisIdRef.current)
        if (!requestedRestore && !current?.run_id && !storedRun?.runId && routeAnalysisId) {
          const historicalRun = await getAnalysisAgentSession(routeAnalysisId)
          if (historicalRun?.run_id) {
            requestedRestore = { runId: historicalRun.run_id, analysisId: routeAnalysisId }
            requestedRunRestoreRef.current = requestedRestore
          }
        }
        const runId = requestedRestore?.runId ?? current?.run_id ?? storedRun?.runId
        if ((forceRestart || adapterChanged) && sessionRef.current) {
          await stopAgentCliSession(sessionRef.current.terminal_session_id).catch(() => undefined)
          sessionRef.current = null
          window.sessionStorage.removeItem(terminalSessionStorageKey)
        }
        let response: Awaited<ReturnType<typeof getAgentCliSession>> | null = null
        let contextPreparedByCreate = false
        if (!forceRestart && stored) {
          onStateChange('connecting', `正在向后端恢复已保存的 ${adapterDisplayName} PTY。`)
          try {
            response = await getAgentCliSession(stored.terminalSessionId, currentPageSessionId)
          } catch (error) {
            const missing = error instanceof ApiError
              && (error.statusCode === 404 || ['AGENT_CLI_SESSION_NOT_FOUND', 'KIMI_CLI_SESSION_NOT_FOUND'].includes(error.code))
            if (!missing) throw error
          }
        }
        const reattachedExistingSession = Boolean(response?.terminal.state === 'running')
        if (!response || response.terminal.state !== 'running') {
          onStateChange('connecting', `正在请求后端创建或恢复 ${adapterDisplayName} PTY 进程。`)
          const create = (candidateRunId?: string) => createAgentCliSession({
            adapterId,
            runId: candidateRunId,
            adoptPageSession: Boolean(requestedRestore && candidateRunId === requestedRestore.runId),
            pageSessionId: currentPageSessionId,
            analysisId: normalizedAnalysisId(requestedRestore?.analysisId ?? analysisIdRef.current) ?? undefined,
            goal: '持续协助用户完成鸿蒙异构与并行优化分析。',
            mode: 'page_agent',
            context: contextRef.current,
            config: configRef.current,
            cols: terminal.cols || 100,
            rows: terminal.rows || 32,
          })
          try {
            response = await create(runId)
            contextPreparedByCreate = true
          } catch (error) {
            if (!(runId && error instanceof ApiError && ['PAGE_AGENT_SESSION_MISMATCH', 'AGENT_RUN_NOT_FOUND'].includes(error.code))) throw error
            clearStoredPageRunBinding()
            response = await create()
            contextPreparedByCreate = true
          }
        }
        onStateChange('connecting', `${adapterDisplayName} Session 已返回，正在连接可恢复输出流。`)
        // React Strict Mode and hot reload may replace xterm while the HTTP
        // preparation above is in flight. Never bind a completed request to a
        // terminal instance that is no longer the visible one.
        if (disposedRef.current || terminalRef.current !== terminal) return
        if (sessionRef.current?.terminal_session_id !== response.terminal.terminal_session_id) {
          lastContextSyncKeyRef.current = ''
        }
        sessionRef.current = response.terminal
        requestedRunRestoreRef.current = null
        if (contextPreparedByCreate) {
          // Creation used the context captured before its HTTP request. The
          // selected device may have changed while awaiting the response, so
          // the next command must synchronize its own current task snapshot.
          lastContextSyncKeyRef.current = ''
        }
        // A reattached Kimi TUI history contains cursor moves tied to its old
        // terminal dimensions. Capture the live cursor, force one repaint,
        // then poll from that cursor so the repaint cannot race ahead of the
        // browser consumer. A newly created session still needs its startup screen.
        outputPollControllerRef.current?.abort()
        terminal.reset()
        terminal.scrollToBottom()
        outputTailRef.current = ''
        latestSequenceRef.current = reattachedExistingSession ? response.terminal.latest_sequence : 0
        storeSession(response.terminal, currentPageSessionId)
        if (response.recovery?.status === 'durable_run_recovered') {
          onStateChange('connecting', '已找回当前页面的持久 Run，正在恢复原 Agent 会话与终端。')
        }
        onSessionReady(response.terminal, response.run)
        onTokenUsage(response.terminal.token_usage)
        startUsagePolling(response.terminal, currentPageSessionId)
        const fittedCols = terminal.cols || response.terminal.cols || 100
        const fittedRows = terminal.rows || response.terminal.rows || 32
        lastBackendSizeRef.current = `${fittedCols}x${fittedRows}`
        try {
          if (reattachedExistingSession) {
            await redrawAgentCliSession(response.terminal.terminal_session_id, fittedCols, fittedRows)
            if (disposedRef.current || terminalRef.current !== terminal) return
            openOutputStream(response.terminal)
          } else {
            openOutputStream(response.terminal)
            await resizeAgentCliSession(response.terminal.terminal_session_id, fittedCols, fittedRows)
            if (disposedRef.current || terminalRef.current !== terminal) return
          }
        } catch (error) {
          lastBackendSizeRef.current = ''
          throw error
        }
        setReady(true)
        recoveryActiveRef.current = false
        recoveryAttemptRef.current = 0
        if (recoveryTimerRef.current) clearTimeout(recoveryTimerRef.current)
        recoveryTimerRef.current = null
        terminal.focus()
      } catch (error) {
        const retryableStatus = error instanceof ApiError
          ? error.statusCode === null || error.statusCode === 404 || error.statusCode === 409 || error.statusCode >= 500
          : true
        if (retryableStatus && connectedRef.current && !disposedRef.current) {
          recoveryActiveRef.current = true
          if (!sessionRef.current) setReady(false)
          const diagnostic = error instanceof Error && error.message.trim() ? `：${error.message.trim()}` : ''
          const attempt = recoveryAttemptRef.current + 1
          const disconnectedAt = transportDisconnectedAtRef.current ?? new Date().toISOString()
          transportDisconnectedAtRef.current = disconnectedAt
          const transportState = ptyTransportFailureState(disconnectedAt)
          emitTransportStatus(
            transportState,
            `${transportState === 'offline' ? '浏览器 PTY 输出链路持续不可用' : '浏览器 PTY 输出链路暂时不可用'}${diagnostic} 已保留页面 Run，正在进行第 ${attempt} 次恢复；后端 Session 不设置空闲过期。`,
            attempt,
          )
          onStateChange('connecting', `后端连接暂时中断${diagnostic} 正在自动重试 ${adapterDisplayName} Session。`)
          scheduleRecoveryRef.current?.()
        } else {
          recoveryActiveRef.current = false
          onStateChange('error', error instanceof Error ? error.message : `无法连接后端 ${adapterDisplayName}。`)
        }
      }
    })()
    connectPromiseRef.current = operation
    try {
      await operation
    } finally {
      if (connectPromiseRef.current === operation) connectPromiseRef.current = null
    }
  }, [adapterDisplayName, adapterId, emitTransportStatus, onSessionReady, onStateChange, onTokenUsage, openOutputStream, startUsagePolling])

  reconnectSessionRef.current = connect

  const ensureActiveSession = useCallback(async (): Promise<AgentCliTerminal> => {
    if (connectPromiseRef.current) await connectPromiseRef.current
    if (!sessionRef.current || sessionRef.current.state !== 'running' || sessionRef.current.adapter_id !== adapterId) await connect(false)
    if (connectPromiseRef.current) await connectPromiseRef.current
    const session = sessionRef.current
    if (!session || session.state !== 'running' || session.adapter_id !== adapterId) throw new Error(`${adapterDisplayName} PTY 尚未连接。`)
    return session
  }, [adapterDisplayName, adapterId, connect])

  const submitCommand = useCallback(async (
    command: string,
    options?: { commandId?: string } & AgentContextSyncOptions,
  ) => {
    const commandId = options?.commandId ?? randomUuid()
    const assertTarget = createAgentSubmissionGuard(() => ({
      ...submissionTargetRef.current,
      sessionId: sessionRef.current?.adapter_id === submissionTargetRef.current.adapterId ? sessionRef.current.terminal_session_id : '',
    }))
    const assertCurrent = () => { options?.assertCurrent?.(); assertTarget() }
    assertCurrent()
    return enqueueAgentSubmission(inputChainRef, async () => {
      try {
        await runGuardedAgentSubmission({
          assertCurrent,
          ensureSession: ensureActiveSession,
          syncContext: (session) => syncContext(session.terminal_session_id, { ...options, assertCurrent }),
          sendCommand: async (session) => {
            await sendAgentCliCommand(session.terminal_session_id, command, commandId)
            onCommandSubmitted({ commandId, submittedAt: new Date().toISOString() })
            onStateChange('working', `页面命令及提交键已写入 ${session.agent_display_name}，等待 Agent 响应。`)
          },
        })
      } catch (error) {
        onStateChange('error', error instanceof Error ? error.message : `写入 ${adapterDisplayName} 失败。`)
        throw error
      }
    })
  }, [adapterDisplayName, ensureActiveSession, onCommandSubmitted, onStateChange, syncContext])

  const pasteTerminalText = useCallback((value: string) => {
    const text = value.replaceAll('\u0000', '')
    if (!text) return
    inputChainRef.current = inputChainRef.current
      .then(async () => {
        const session = await ensureActiveSession()
        const bracketed = session.submit_mode?.startsWith('bracketed_paste_') ?? false
        const framed = bracketed ? `\u001b[200~${text}\u001b[201~` : text
        await writeAgentCliInput(session.terminal_session_id, new TextEncoder().encode(framed))
        terminalRef.current?.focus()
      })
      .catch((error) => onStateChange('error', error instanceof Error ? error.message : `粘贴到 ${adapterDisplayName} 失败。`))
  }, [adapterDisplayName, ensureActiveSession, onStateChange])

  useImperativeHandle(ref, () => ({
    async sendCommand(command, options) {
      await submitCommand(command, options)
      terminalRef.current?.focus()
    },
    async interrupt() {
      const session = sessionRef.current
      if (!session) return
      await writeAgentCliInput(session.terminal_session_id, new Uint8Array([3]))
      onStateChange('ready', `已向 ${session.agent_display_name} 发送 Ctrl+C。`)
    },
    focus() {
      terminalRef.current?.focus()
    },
    async redraw() {
      const session = sessionRef.current
      const terminal = terminalRef.current
      if (!session || !terminal) return
      outputPollControllerRef.current?.abort()
      terminal.reset()
      terminal.scrollToBottom()
      outputTailRef.current = ''
      fitAddonRef.current?.fit()
      const cols = terminal.cols || session.cols || 100
      const rows = terminal.rows || session.rows || 32
      lastBackendSizeRef.current = `${cols}x${rows}`
      // Keep latestSequenceRef at the last observed server cursor. The resize
      // forces Kimi to append a fresh screen after it; opening the poller only
      // after the request guarantees that screen is replayed from the buffer.
      await redrawAgentCliSession(session.terminal_session_id, cols, rows)
      openOutputStream(session)
      terminal.focus()
    },
    async restart() {
      submissionTargetRef.current.generation++
      await connect(true)
    },
    async restoreRun(runId, restoredAnalysisId) {
      const normalizedRunId = runId.trim()
      const normalizedRestoredAnalysisId = restoredAnalysisId.trim()
      if (!normalizedRunId || !normalizedRestoredAnalysisId) return
      analysisIdRef.current = normalizedRestoredAnalysisId
      if (sessionRef.current?.run_id === normalizedRunId) {
        await syncContext(sessionRef.current.terminal_session_id, { analysisId: normalizedRestoredAnalysisId })
        await redrawAgentCliSession(sessionRef.current.terminal_session_id, terminalRef.current?.cols ?? 100, terminalRef.current?.rows ?? 32)
        return
      }
      submissionTargetRef.current.generation++
      requestedRunRestoreRef.current = { runId: normalizedRunId, analysisId: normalizedRestoredAnalysisId }
      outputPollControllerRef.current?.abort()
      sessionRef.current = null
      window.sessionStorage.removeItem(terminalSessionStorageKey)
      const currentPageSessionId = pageSessionIdRef.current ?? pageSessionId()
      pageSessionIdRef.current = currentPageSessionId
      window.sessionStorage.setItem(pageRunBindingStorageKey, JSON.stringify({
        schemaVersion: 'agent_cli_page_run/v1',
        pageSessionId: currentPageSessionId,
        runId: normalizedRunId,
        savedAt: new Date().toISOString(),
      } satisfies StoredPageRunBinding))
      setReady(false)
      await connect(true)
    },
  }), [connect, onStateChange, openOutputStream, submitCommand, syncContext])

  useEffect(() => {
    let cancelled = false
    disposedRef.current = false
    setReady(false)
    let resizeObserver: ResizeObserver | null = null
    let inputDisposable: { dispose: () => void } | null = null
    let terminal: XtermTerminal | null = null
    let terminalMount: HTMLDivElement | null = null
    let terminalPasteListener: ((event: ClipboardEvent) => void) | null = null
    let windowResizeListener: (() => void) | null = null
    void Promise.all([import('@xterm/xterm'), import('@xterm/addon-fit')]).then(([xterm, fit]) => {
      if (cancelled || disposedRef.current || !mountRef.current) return
      terminalMount = mountRef.current
      // React Strict Mode mounts, cleans up, and mounts effects again in
      // development. An already resolved dynamic import from the cancelled
      // effect must never leave an empty xterm layer over the live terminal.
      terminalMount.replaceChildren()
      terminal = new xterm.Terminal({
        cursorBlink: true,
        cursorStyle: 'block',
        convertEol: false,
        scrollback: 12_000,
        fontFamily: '"SFMono-Regular", "Cascadia Code", "Liberation Mono", Consolas, monospace',
        fontSize: 12,
        lineHeight: 1.22,
        letterSpacing: 0,
        allowTransparency: false,
        theme: {
          background: '#111315',
          foreground: '#e8eaed',
          cursor: '#4da3ff',
          cursorAccent: '#111315',
          selectionBackground: '#285b86',
          black: '#111315',
          brightBlack: '#747b84',
          blue: '#62a8ff',
          brightBlue: '#8fc1ff',
          cyan: '#55c2c3',
          green: '#68c27a',
          yellow: '#e1b655',
          red: '#ef6a67',
          magenta: '#bd8be0',
          white: '#e8eaed',
        },
      })
      const fitAddon = new fit.FitAddon()
      terminal.loadAddon(fitAddon)
      terminal.open(terminalMount)
      fitAddon.fit()
      terminalRef.current = terminal
      fitAddonRef.current = fitAddon
      inputDisposable = terminal.onData(queueInput)
      terminal.attachCustomKeyEventHandler((event) => {
        const pasteShortcut = event.type === 'keydown'
          && (event.ctrlKey || event.metaKey)
          && !event.altKey
          && event.key.toLowerCase() === 'v'
        // Let the browser emit a native paste event. The capture listener
        // below reads ClipboardEvent data directly, so this also works when a
        // remote deployment is served over plain HTTP and Clipboard API access
        // is unavailable.
        return !pasteShortcut
      })
      terminalPasteListener = (event) => {
        const text = event.clipboardData?.getData('text/plain') ?? ''
        if (!text) return
        event.preventDefault()
        event.stopPropagation()
        pasteTerminalText(text)
      }
      terminalMount.addEventListener('paste', terminalPasteListener, true)
      const fitAndSync = () => {
        if (resizeTimerRef.current) clearTimeout(resizeTimerRef.current)
        resizeTimerRef.current = setTimeout(() => {
          if (cancelled || disposedRef.current) return
          fitAddon.fit()
          const session = sessionRef.current
          if (!session) return
          const cols = terminal?.cols ?? 100
          const rows = terminal?.rows ?? 32
          const size = `${cols}x${rows}`
          if (lastBackendSizeRef.current === size) return
          lastBackendSizeRef.current = size
          void resizeAgentCliSession(session.terminal_session_id, cols, rows).catch(() => {
            if (lastBackendSizeRef.current === size) lastBackendSizeRef.current = ''
          })
        }, 80)
      }
      resizeObserver = new ResizeObserver(fitAndSync)
      resizeObserver.observe(terminalMount)
      windowResizeListener = fitAndSync
      window.addEventListener('resize', windowResizeListener)
      void document.fonts.ready.then(() => fitAndSync())
      if (connectedRef.current) void connect()
    })
    return () => {
      cancelled = true
      disposedRef.current = true
      const currentPoll = outputPollControllerRef.current
      currentPoll?.abort()
      if (outputPollControllerRef.current === currentPoll) outputPollControllerRef.current = null
      if (resizeTimerRef.current) clearTimeout(resizeTimerRef.current)
      if (inputTimerRef.current) clearTimeout(inputTimerRef.current)
      if (idleTimerRef.current) clearTimeout(idleTimerRef.current)
      if (usagePollTimerRef.current) clearInterval(usagePollTimerRef.current)
      if (recoveryTimerRef.current) clearTimeout(recoveryTimerRef.current)
      recoveryTimerRef.current = null
      resizeObserver?.disconnect()
      if (windowResizeListener) window.removeEventListener('resize', windowResizeListener)
      if (terminalPasteListener && terminalMount) terminalMount.removeEventListener('paste', terminalPasteListener, true)
      inputDisposable?.dispose()
      terminal?.dispose()
      if (terminalRef.current === terminal) {
        terminalRef.current = null
        fitAddonRef.current = null
      }
    }
  }, [connect, pasteTerminalText, queueInput])

  useEffect(() => {
    if (connected && terminalRef.current && !sessionRef.current) {
      void connect()
      return
    }
    if (!connected && !sessionRef.current) {
      emitTransportStatus('offline', waitingDetail || `${adapterDisplayName} 尚不可用，PTY Session 尚未创建。`)
    }
  }, [adapterDisplayName, connect, connected, emitTransportStatus, waitingDetail])

  useEffect(() => {
    const session = sessionRef.current
    if (!connected || !terminalRef.current || !session) return
    void syncContext(session.terminal_session_id, { analysisId })
  }, [analysisId, connected, syncContext])

  return (
    <div className={`kimi-webshell ${ready ? 'is-ready' : ''}`}>
      <div ref={mountRef} className="kimi-webshell-terminal" aria-label={`连接后端真实 ${adapterDisplayName} 的 WebShell`} />
      <AgentLocalComposer
        adapterDisplayName={adapterDisplayName}
        ready={ready}
        onSubmit={submitCommand}
      />
      {!ready && <div className="kimi-webshell-loading">{waitingDetail || (connected ? `正在连接真实 ${adapterDisplayName} PTY…` : `正在等待 ${adapterDisplayName} 可用；主工作台仍可使用。`)}</div>}
    </div>
  )
})

export type KimiWebShellHandle = AgentWebShellHandle
export const KimiWebShell = AgentWebShell
