'use client'

import { randomUuid } from './random-id'

export type AgentCommand = {
  message: string
  displayMessage?: string
  analysisId?: string
  requestId?: string
}

const eventName = 'page-agent-command'
export const agentCommandStatusEventName = 'page-agent-command-status'
export const agentAutonomousTaskStatusEventName = 'page-agent-autonomous-task-status'

export type AgentCommandStatus = {
  requestId: string
  status: 'accepted' | 'rejected'
  message?: string
}

export type AgentAutonomousTaskStatus = AgentCommandStatus

/** Submit a UI intent to the one persistent page Agent Session. */
export function dispatchAgentCommand(command: AgentCommand): string {
  if (typeof window === 'undefined') return ''
  const message = command.message.trim()
  if (!message) return ''
  const requestId = command.requestId?.trim() || randomUuid()
  window.dispatchEvent(new CustomEvent(eventName, {
    detail: {
      request_id: requestId,
      message,
      display_message: command.displayMessage?.trim() || message,
      analysis_id: command.analysisId?.trim() || '',
    },
  }))
  return requestId
}

export const agentCommandEventName = eventName

function reportStatus(event: string, status: AgentCommandStatus): void {
  if (typeof window === 'undefined') return
  window.dispatchEvent(new CustomEvent(event, {
    detail: {
      request_id: status.requestId,
      status: status.status,
      message: status.message?.trim() ?? '',
    },
  }))
}

export function reportAgentCommandStatus(status: AgentCommandStatus): void {
  reportStatus(agentCommandStatusEventName, status)
}

export function reportAgentAutonomousTaskStatus(status: AgentAutonomousTaskStatus): void {
  reportStatus(agentAutonomousTaskStatusEventName, status)
}
