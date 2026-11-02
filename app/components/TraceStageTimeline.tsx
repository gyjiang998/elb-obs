'use client'

import { Check, ChevronRight, FileCode2, LoaderCircle } from 'lucide-react'
import { useMemo, useState } from 'react'
import type { JsonRecord } from '../lib/types'

type StageEvent = {
  key: string
  eventId: string
  name: string
  timestampMs: number | null
  durationMs: number | null
  processName: string
  threadName: string
  occurrenceCount: number | null
  selfTimeEstimateTotalMs: number | null
  inclusiveTotalMs: number | null
  maxSelfTimeEstimateMs: number | null
  maxSpanMs: number | null
  summary: string
}

type TimelineOccurrence = {
  key: string
  eventId: string
  name: string
  rank: number | null
  timestampMs: number
  durationMs: number
  selfTimeEstimateMs: number | null
  processName: string
  threadName: string
  lane: number
}

type TimelineDensityBucket = {
  key: string
  startMs: number
  endMs: number
  occurrenceCount: number
  inclusiveTotalMs: number
  selfTimeEstimateTotalMs: number
}

type EventTimeline = {
  representation: string
  sourceOccurrenceCount: number
  returnedOccurrenceCount: number
  truncated: boolean
  occurrences: TimelineOccurrence[]
  densityBuckets: TimelineDensityBucket[]
  semantics: string
}

type StageRow = {
  stageId: string
  stageName: string
  stageOrder: number
  sourceIndex: number
  nodeType: 'action' | 'phase' | 'stage' | 'activity'
  parentId: string
  startNs: number | null
  endNs: number | null
  durationMs: number | null
  reportedEventCount: number
  reportedTotalEventDurationMs: number | null
  listedEventCount: number
  coverage: string
  startEvent: JsonRecord
  endEvent: JsonRecord
  events: StageEvent[]
  eventTimeline: EventTimeline
}

function record(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {}
}

function records(value: unknown): JsonRecord[] {
  return Array.isArray(value) ? value.map(record).filter((item) => Object.keys(item).length > 0) : []
}

function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' && value.trim() ? value.trim() : fallback
}

function finiteNumber(value: unknown): number | null {
  const parsed = typeof value === 'number' ? value : typeof value === 'string' && value.trim() ? Number(value) : Number.NaN
  return Number.isFinite(parsed) ? parsed : null
}

function integer(value: unknown, fallback = 0): number {
  const parsed = finiteNumber(value)
  return parsed === null ? fallback : Math.max(0, Math.trunc(parsed))
}

function eventFromRecord(value: JsonRecord, stageId: string, index: number): StageEvent {
  const metrics = record(value.metrics)
  const occurrences = records(value.observed_occurrences)
  const firstOccurrence = occurrences[0] ?? {}
  const eventId = text(value.event_id, text(value.hotspot_id, ''))
  const timestampMs = finiteNumber(value.ts_ms) ?? finiteNumber(firstOccurrence.ts_ms)
  const durationMs = finiteNumber(value.dur_ms)
    ?? finiteNumber(metrics.total_duration_ms)
    ?? finiteNumber(metrics.active_duration_ms)
    ?? finiteNumber(firstOccurrence.dur_ms)
  const occurrenceCount = finiteNumber(metrics.listed_occurrence_count)
    ?? finiteNumber(metrics.occurrence_count)
    ?? finiteNumber(metrics.call_count)
    ?? (occurrences.length > 0 ? occurrences.length : null)
  return {
    key: `${stageId}:${eventId || text(value.name, text(value.event_name, 'event'))}:${index}`,
    eventId,
    name: text(value.event_name, text(value.name, text(firstOccurrence.name, '未命名 Trace 事件'))),
    timestampMs,
    durationMs,
    processName: text(value.process_name, text(value.module, text(firstOccurrence.process_name, ''))),
    threadName: text(value.thread_name, text(firstOccurrence.thread_name, '')),
    occurrenceCount,
    selfTimeEstimateTotalMs: finiteNumber(metrics.self_time_estimate_total_duration_ms),
    inclusiveTotalMs: finiteNumber(metrics.inclusive_total_duration_ms),
    maxSelfTimeEstimateMs: finiteNumber(metrics.max_self_time_estimate_duration_ms),
    maxSpanMs: finiteNumber(metrics.max_span_duration_ms),
    summary: text(value.summary, ''),
  }
}

function assignOccurrenceLanes(occurrences: Omit<TimelineOccurrence, 'lane'>[], laneCount = 4): TimelineOccurrence[] {
  const laneEnds = Array.from({ length: laneCount }, () => Number.NEGATIVE_INFINITY)
  return [...occurrences]
    .sort((left, right) => left.timestampMs - right.timestampMs || right.durationMs - left.durationMs || left.name.localeCompare(right.name))
    .map((occurrence) => {
      let lane = laneEnds.findIndex((end) => end <= occurrence.timestampMs)
      if (lane < 0) {
        lane = laneEnds.reduce((earliestLane, end, index) => end < laneEnds[earliestLane] ? index : earliestLane, 0)
      }
      laneEnds[lane] = Math.max(laneEnds[lane], occurrence.timestampMs + occurrence.durationMs)
      return { ...occurrence, lane }
    })
}

function normalizeEventTimeline(value: JsonRecord, stageId: string, events: StageEvent[]): EventTimeline {
  const timeline = record(value.event_timeline)
  const explicitOccurrences = records(timeline.occurrences).flatMap((item, index) => {
    const timestampMs = finiteNumber(item.ts_ms)
    const durationMs = finiteNumber(item.dur_ms)
    if (timestampMs === null || durationMs === null) return []
    return [{
      key: `${stageId}:timeline:${text(item.event_id, text(item.name, 'event'))}:${index}`,
      eventId: text(item.event_id),
      name: text(item.name, '未命名 Trace 事件'),
      rank: finiteNumber(item.rank),
      timestampMs,
      durationMs: Math.max(0, durationMs),
      selfTimeEstimateMs: finiteNumber(item.self_time_estimate_duration_ms),
      processName: text(item.process_name),
      threadName: text(item.thread_name),
    }]
  })
  const representativeOccurrences = events.flatMap((event, index) => {
    if (event.timestampMs === null || event.durationMs === null) return []
    return [{
      key: `${stageId}:representative:${event.eventId || event.name}:${index}`,
      eventId: event.eventId,
      name: event.name,
      rank: index + 1,
      timestampMs: event.timestampMs,
      durationMs: Math.max(0, event.durationMs),
      selfTimeEstimateMs: event.maxSelfTimeEstimateMs,
      processName: event.processName,
      threadName: event.threadName,
    }]
  })
  const occurrences = assignOccurrenceLanes(explicitOccurrences.length > 0 ? explicitOccurrences : representativeOccurrences)
  const densityBuckets = records(timeline.density_buckets).flatMap((item, index) => {
    const startMs = finiteNumber(item.start_ms)
    const endMs = finiteNumber(item.end_ms)
    if (startMs === null || endMs === null || endMs <= startMs) return []
    return [{
      key: `${stageId}:density:${integer(item.index, index)}`,
      startMs,
      endMs,
      occurrenceCount: integer(item.occurrence_count),
      inclusiveTotalMs: finiteNumber(item.inclusive_total_duration_ms) ?? 0,
      selfTimeEstimateTotalMs: finiteNumber(item.self_time_estimate_total_duration_ms) ?? 0,
    }]
  })
  const fallbackSourceCount = events.reduce((total, event) => total + (event.occurrenceCount ?? 0), 0)
  return {
    representation: text(timeline.representation, representativeOccurrences.length > 0 ? 'representative_occurrences' : 'unavailable'),
    sourceOccurrenceCount: integer(timeline.source_occurrence_count, fallbackSourceCount),
    returnedOccurrenceCount: integer(timeline.returned_occurrence_count, occurrences.length),
    truncated: typeof timeline.truncated === 'boolean' ? timeline.truncated : fallbackSourceCount > representativeOccurrences.length,
    occurrences,
    densityBuckets,
    semantics: text(timeline.semantics, representativeOccurrences.length > 0 ? '橙色区间为每个 Top Event 的代表性调用。' : ''),
  }
}

function normalizeStage(value: JsonRecord, index: number): StageRow {
  const stageId = text(value.stage_id, text(value.id, `stage_${index + 1}`))
  const timeRange = record(value.time_range_ns)
  const topEvents = records(value.top_events)
  const groupedEvents = records(value.events)
  const eventSource = groupedEvents.length > 0 ? groupedEvents : topEvents
  const startEvent = record(value.start_event)
  const endEvent = record(value.end_event)
  const events = eventSource.map((event, eventIndex) => eventFromRecord(event, stageId, eventIndex))
  const startEventMs = finiteNumber(startEvent.ts_ms)
  const startNs = startEventMs === null ? finiteNumber(timeRange.start_ns) ?? finiteNumber(value.start_ns) : startEventMs * 1_000_000
  let endNs = finiteNumber(timeRange.end_ns) ?? finiteNumber(value.end_ns)
  const durationMs = finiteNumber(value.duration_ms)
  if (endNs === null && startNs !== null && durationMs !== null) endNs = startNs + durationMs * 1_000_000
  return {
    stageId,
    stageName: text(value.stage_name, text(value.name, stageId)),
    stageOrder: integer(value.stage_order ?? value.order, index + 1),
    sourceIndex: index,
    nodeType: (['action', 'phase', 'stage', 'activity'].includes(text(value.type))
      ? text(value.type)
      : 'stage') as StageRow['nodeType'],
    parentId: text(value.parent_id),
    startNs,
    endNs,
    durationMs: durationMs ?? (startNs !== null && endNs !== null && endNs >= startNs ? (endNs - startNs) / 1_000_000 : null),
    reportedEventCount: integer(value.reported_event_count ?? value.event_count),
    reportedTotalEventDurationMs: finiteNumber(value.reported_total_event_duration_ms ?? value.total_event_duration_ms),
    listedEventCount: integer(value.returned_event_count ?? value.listed_top_event_count, eventSource.length),
    coverage: text(value.coverage, eventSource.length > 0 ? 'representative_top_events_only' : 'no_event_anchor'),
    startEvent,
    endEvent,
    events,
    eventTimeline: normalizeEventTimeline(value, stageId, events),
  }
}

function formatMilliseconds(value: number | null): string {
  if (value === null) return '未提供'
  if (value >= 1000) return `${(value / 1000).toFixed(2)} s`
  if (value >= 10) return `${value.toFixed(2)} ms`
  return `${value.toFixed(3)} ms`
}

function timestampOffset(timestampMs: number | null, stage: StageRow): string {
  if (timestampMs === null) return '未提供'
  if (stage.startNs === null) return `${timestampMs.toFixed(3)} ms`
  return `+${Math.max(0, timestampMs - stage.startNs / 1_000_000).toFixed(3)} ms`
}

function eventOffset(event: StageEvent, stage: StageRow): string {
  return timestampOffset(event.timestampMs, stage)
}

function anchorLabel(anchor: JsonRecord): string {
  const name = text(anchor.name, '')
  if (!name) return '未提供'
  const timestamp = finiteNumber(anchor.ts_ms)
  return timestamp === null ? name : `${name} @ ${timestamp.toFixed(3)} ms`
}

function timelineRange(
  startMs: number,
  durationMs: number,
  stage: StageRow,
  bounds: { startNs: number; endNs: number },
): { left: number; width: number } | null {
  const boundsStartMs = bounds.startNs / 1_000_000
  const boundsEndMs = bounds.endNs / 1_000_000
  const stageStartMs = stage.startNs === null ? boundsStartMs : stage.startNs / 1_000_000
  const stageEndMs = stage.durationMs !== null
    ? stageStartMs + stage.durationMs
    : stage.endNs === null ? boundsEndMs : stage.endNs / 1_000_000
  const clippedStartMs = Math.max(boundsStartMs, stageStartMs, startMs)
  const clippedEndMs = Math.min(boundsEndMs, stageEndMs, startMs + Math.max(0, durationMs))
  if (clippedEndMs <= clippedStartMs) return null
  const windowMs = Math.max(0.001, boundsEndMs - boundsStartMs)
  const left = Math.max(0, Math.min(100, ((clippedStartMs - boundsStartMs) / windowMs) * 100))
  const stageRight = Math.max(left, Math.min(100, ((Math.min(boundsEndMs, stageEndMs) - boundsStartMs) / windowMs) * 100))
  const rawWidth = ((clippedEndMs - clippedStartMs) / windowMs) * 100
  const width = Math.min(stageRight - left, Math.max(0.12, rawWidth))
  return width > 0 ? { left, width } : null
}

export function TraceStageTimeline({
  stages,
  nodes = [],
  selectedEventId = '',
  busyEventId = '',
  onSelectEvent,
}: {
  stages: JsonRecord[]
  nodes?: JsonRecord[]
  selectedEventId?: string
  busyEventId?: string
  onSelectEvent?: (eventId: string) => void
}) {
  const sourceRows = nodes.length > 0 ? nodes : stages
  const rows = useMemo(
    () => sourceRows.map(normalizeStage),
    [sourceRows],
  )
  const [expandedStageIds, setExpandedStageIds] = useState<string[]>([])
  const visibleRows = useMemo(() => {
    const rowById = new Map(rows.map((row) => [row.stageId, row]))
    const childrenByParent = new Map<string, StageRow[]>()
    rows.forEach((row) => {
      if (!row.parentId || !rowById.has(row.parentId)) return
      const children = childrenByParent.get(row.parentId) ?? []
      children.push(row)
      childrenByParent.set(row.parentId, children)
    })
    childrenByParent.forEach((children) => children.sort(
      (left, right) => left.stageOrder - right.stageOrder || left.sourceIndex - right.sourceIndex,
    ))
    const phaseRoots = rows
      .filter((row) => row.nodeType === 'phase')
      .sort((left, right) => left.sourceIndex - right.sourceIndex)
    const roots = phaseRoots.length > 0
      ? phaseRoots
      : rows
        .filter((row) => !row.parentId || !rowById.has(row.parentId))
        .sort((left, right) => left.stageOrder - right.stageOrder || left.sourceIndex - right.sourceIndex)
    const visible: Array<{ row: StageRow; depth: number; childCount: number }> = []
    const append = (row: StageRow, depth: number) => {
      const children = childrenByParent.get(row.stageId) ?? []
      visible.push({ row, depth, childCount: children.length })
      if (expandedStageIds.includes(row.stageId)) {
        children.forEach((child) => append(child, depth + 1))
      }
    }
    roots.forEach((row) => append(row, 0))
    return visible
  }, [expandedStageIds, rows])
  const bounds = useMemo(() => {
    const starts = rows.flatMap((row) => row.startNs === null ? [] : [row.startNs])
    const ends = rows.flatMap((row) => row.endNs === null ? [] : [row.endNs])
    if (starts.length === 0) return null
    const startNs = Math.min(...starts)
    const endNs = Math.max(startNs + 1_000_000, ...ends, ...starts)
    return { startNs, endNs, durationMs: (endNs - startNs) / 1_000_000 }
  }, [rows])

  if (rows.length === 0) return null

  const nodeTypeLabels: Record<StageRow['nodeType'], string> = {
    action: '用户动作',
    phase: '大阶段',
    stage: '小阶段',
    activity: '细粒度活动',
  }
  const nodeCounts = rows.reduce<Record<StageRow['nodeType'], number>>(
    (counts, row) => ({ ...counts, [row.nodeType]: counts[row.nodeType] + 1 }),
    { action: 0, phase: 0, stage: 0, activity: 0 },
  )

  return <div className="trace-stage-timeline" aria-label="按层级阶段展示的 Trace 热点时间轴">
    <div className="trace-stage-timeline-note">
      <div><span>节点条按 start event 在全流程中的相对位置对齐；父级跨度取子节点墙钟包络，Self 与 Inclusive 保留各自测量语义。</span><span className="trace-stage-legend"><i className="stage" />节点 Wall Duration<i className="event" />热点事件调用</span></div>
      <strong>{nodeCounts.phase} 大阶段 · {nodeCounts.stage} 小阶段 · {nodeCounts.activity} 活动</strong>
    </div>
    <div className="trace-stage-table">
      <div className="trace-stage-header">
        <span>层级阶段</span><span>墙钟包络</span>
        <span className="trace-stage-axis"><i>0 ms</i><i>{formatMilliseconds(bounds?.durationMs ?? null)}</i></span>
        <span>热点覆盖</span>
      </div>
      {visibleRows.map(({ row: stage, depth, childCount }) => {
        const expanded = expandedStageIds.includes(stage.stageId)
        const startPercent = bounds && stage.startNs !== null
          ? Math.max(0, Math.min(100, ((stage.startNs - bounds.startNs) / (bounds.endNs - bounds.startNs)) * 100))
          : null
        const barDurationNs = stage.durationMs === null ? stage.startNs !== null && stage.endNs !== null ? stage.endNs - stage.startNs : null : stage.durationMs * 1_000_000
        const widthPercent = bounds && stage.startNs !== null && barDurationNs !== null
          ? Math.max(0.7, Math.min(100 - (startPercent ?? 0), (barDurationNs / (bounds.endNs - bounds.startNs)) * 100))
          : null
        const maxDensityCount = Math.max(1, ...stage.eventTimeline.densityBuckets.map((bucket) => bucket.occurrenceCount))
        const missingOccurrences = Math.max(0, stage.reportedEventCount - stage.listedEventCount)
        return <section className={`trace-stage-row ${expanded ? 'expanded' : ''}`} key={stage.stageId}>
          <button className="trace-stage-summary" type="button" aria-expanded={expanded} onClick={() => setExpandedStageIds((current) => expanded ? current.filter((id) => id !== stage.stageId) : [...current, stage.stageId])}>
            <span className="trace-stage-name" style={{ paddingLeft: `${depth * 18}px` }}><ChevronRight size={15} /><span><strong>{stage.stageName}</strong><small><b>{nodeTypeLabels[stage.nodeType]}</b> · #{stage.stageOrder} · {stage.stageId}{childCount > 0 ? ` · ${childCount} 个子节点` : ''}</small></span></span>
            <span className="trace-stage-duration"><strong>{formatMilliseconds(stage.durationMs)}</strong><small>事件累计 {formatMilliseconds(stage.reportedTotalEventDurationMs)}</small></span>
            <span className="trace-stage-track" title={`开始：${anchorLabel(stage.startEvent)}；结束：${anchorLabel(stage.endEvent)}`}>
              {startPercent !== null && <i className="trace-stage-start-marker" style={{ left: `${startPercent}%` }} />}
              {startPercent !== null && widthPercent !== null && <i className="trace-stage-duration-bar" style={{ left: `${startPercent}%`, width: `${widthPercent}%` }} />}
              {bounds && startPercent !== null && widthPercent !== null && stage.eventTimeline.occurrences.map((occurrence) => {
                const range = timelineRange(occurrence.timestampMs, occurrence.durationMs, stage, bounds)
                if (!range) return null
                const title = `${occurrence.name}\n相对阶段起点 ${timestampOffset(occurrence.timestampMs, stage)} · Span ${formatMilliseconds(occurrence.durationMs)}${occurrence.selfTimeEstimateMs === null ? '' : ` · 估算 Self ${formatMilliseconds(occurrence.selfTimeEstimateMs)}`}\n${occurrence.processName || '未知进程'} / ${occurrence.threadName || '未知线程'}`
                return <i className="trace-stage-event-span" key={occurrence.key} title={title} style={{ left: `${range.left}%`, width: `${range.width}%`, top: `${7 + occurrence.lane * 3}px` }} />
              })}
              {bounds && startPercent !== null && widthPercent !== null && stage.eventTimeline.densityBuckets.map((bucket) => {
                const range = timelineRange(bucket.startMs, bucket.endMs - bucket.startMs, stage, bounds)
                if (!range) return null
                const intensity = bucket.occurrenceCount / maxDensityCount
                return <i className="trace-stage-event-density" key={bucket.key} title={`${bucket.occurrenceCount} 次热点调用\n累计估算 Self ${formatMilliseconds(bucket.selfTimeEstimateTotalMs)} · Inclusive ${formatMilliseconds(bucket.inclusiveTotalMs)}`} style={{ left: `${range.left}%`, width: `${range.width}%`, height: `${3 + intensity * 9}px`, opacity: 0.4 + intensity * 0.45 }} />
              })}
              {startPercent === null && <em>缺少阶段起点</em>}
            </span>
            <span className="trace-stage-coverage"><strong>已列 {stage.listedEventCount} / 报告 {stage.reportedEventCount}</strong><small>{missingOccurrences > 0 ? `${missingOccurrences} 条未包含在输入 JSON` : stage.coverage === 'stage_boundary_anchors_only' ? '仅有边界锚点' : '输入记录完整展示'}</small></span>
          </button>
          {expanded && <div className="trace-stage-detail">
            <div className="trace-stage-anchors"><span><b>Start edge</b>{anchorLabel(stage.startEvent)}</span><span><b>End edge</b>{anchorLabel(stage.endEvent)}</span></div>
            {text(stage.startEvent.event_id) && text(stage.startEvent.event_id) === text(stage.endEvent.event_id) && <div className="trace-stage-shared-scope">同一 Scope Trace 的起始边缘与结束边缘共同形成该节点包络。</div>}
            <div className="trace-stage-timeline-semantics"><b>事件分布</b><span>{stage.eventTimeline.semantics || '当前阶段没有可绘制的热点调用区间。'}</span><small>{stage.eventTimeline.representation === 'binned_density' ? `${stage.eventTimeline.sourceOccurrenceCount} 次调用已聚合为 ${stage.eventTimeline.densityBuckets.length} 个非空时间桶` : `${stage.eventTimeline.returnedOccurrenceCount} 个区间${stage.eventTimeline.truncated ? `，来自 ${stage.eventTimeline.sourceOccurrenceCount} 次调用` : ''}`}</small></div>
            {stage.events.length > 0 ? <div className="trace-stage-events">
              <div className="trace-stage-event-header"><span>热点事件</span><span>相对阶段起点</span><span>代表跨度</span><span>累计 Self / Inclusive</span><span>进程 / 线程</span><span>次数</span><span /></div>
              {stage.events.map((event) => <div className={`trace-stage-event ${event.eventId && event.eventId === selectedEventId ? 'selected' : ''}`} key={event.key}>
                <span><strong>{event.name}</strong>{event.summary && <small>{event.summary}</small>}</span>
                <span>{eventOffset(event, stage)}</span>
                <span>{formatMilliseconds(event.durationMs)}</span>
                <span className="trace-stage-event-metrics"><strong>Self {formatMilliseconds(event.selfTimeEstimateTotalMs)}</strong><small>Inclusive {formatMilliseconds(event.inclusiveTotalMs)} · 最大 Self {formatMilliseconds(event.maxSelfTimeEstimateMs)}</small></span>
                <span><strong>{event.processName || '未提供'}</strong><small>{event.threadName || '未提供线程'}</small></span>
                <span>{event.occurrenceCount ?? 1}</span>
                {onSelectEvent && event.eventId ? <button className="icon-button" type="button" title="解析此热点的源码锚点" aria-label="解析此热点的源码锚点" disabled={busyEventId === event.eventId} onClick={() => onSelectEvent(event.eventId)}>{busyEventId === event.eventId ? <LoaderCircle size={15} className="spin" /> : event.eventId === selectedEventId ? <Check size={15} /> : <FileCode2 size={15} />}</button> : <span />}
              </div>)}
            </div> : <div className="trace-stage-empty">该阶段没有携带热点事件；仍保留阶段时间边界和报告计数。</div>}
          </div>}
        </section>
      })}
    </div>
  </div>
}
