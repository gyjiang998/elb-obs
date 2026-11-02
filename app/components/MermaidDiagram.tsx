'use client'

import { CircleAlert, LoaderCircle, Maximize2, RotateCcw, ZoomIn, ZoomOut } from 'lucide-react'
import { useCallback, useEffect, useId, useRef, useState, type PointerEvent as ReactPointerEvent, type WheelEvent as ReactWheelEvent } from 'react'

const MIN_SCALE = 0.25
const MAX_SCALE = 3

type DiagramSize = { width: number; height: number }
type DragState = { pointerId: number; x: number; y: number; left: number; top: number }

function clampScale(value: number): number {
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, value))
}

function svgSize(value: string): DiagramSize {
  const document = new DOMParser().parseFromString(value, 'image/svg+xml')
  const root = document.documentElement
  const viewBox = (root.getAttribute('viewBox') ?? '').trim().split(/[\s,]+/).map(Number)
  if (viewBox.length === 4 && viewBox.every(Number.isFinite) && viewBox[2] > 0 && viewBox[3] > 0) {
    return { width: viewBox[2], height: viewBox[3] }
  }
  const width = Number.parseFloat(root.getAttribute('width') ?? '')
  const height = Number.parseFloat(root.getAttribute('height') ?? '')
  return {
    width: Number.isFinite(width) && width > 0 ? width : 960,
    height: Number.isFinite(height) && height > 0 ? height : 540,
  }
}

export function MermaidDiagram({ source }: { source: string }) {
  const reactId = useId()
  const viewportRef = useRef<HTMLDivElement | null>(null)
  const dragRef = useRef<DragState | null>(null)
  const [svg, setSvg] = useState('')
  const [error, setError] = useState('')
  const [diagramSize, setDiagramSize] = useState<DiagramSize>({ width: 960, height: 540 })
  const [scale, setScale] = useState(1)
  const [fitMode, setFitMode] = useState(true)
  const [dragging, setDragging] = useState(false)

  const centerViewport = useCallback((nextScale: number, xRatio = 0.5, yRatio = 0.5) => {
    window.requestAnimationFrame(() => {
      const viewport = viewportRef.current
      if (!viewport) return
      const contentWidth = diagramSize.width * nextScale
      const contentHeight = diagramSize.height * nextScale
      viewport.scrollLeft = Math.max(0, contentWidth * xRatio - viewport.clientWidth / 2)
      viewport.scrollTop = Math.max(0, contentHeight * yRatio - viewport.clientHeight / 2)
    })
  }, [diagramSize.height, diagramSize.width])

  const fitDiagram = useCallback((keepFitMode = true) => {
    const viewport = viewportRef.current
    if (!viewport) return
    const availableWidth = Math.max(120, viewport.clientWidth - 32)
    const availableHeight = Math.max(120, viewport.clientHeight - 32)
    const nextScale = clampScale(Math.min(availableWidth / diagramSize.width, availableHeight / diagramSize.height))
    setScale(nextScale)
    if (keepFitMode) setFitMode(true)
    centerViewport(nextScale)
  }, [centerViewport, diagramSize.height, diagramSize.width])

  const changeScale = useCallback((nextValue: number) => {
    const viewport = viewportRef.current
    const contentWidth = diagramSize.width * scale
    const contentHeight = diagramSize.height * scale
    const xRatio = viewport && contentWidth > 0 ? (viewport.scrollLeft + viewport.clientWidth / 2) / contentWidth : 0.5
    const yRatio = viewport && contentHeight > 0 ? (viewport.scrollTop + viewport.clientHeight / 2) / contentHeight : 0.5
    const nextScale = clampScale(nextValue)
    setScale(nextScale)
    setFitMode(false)
    centerViewport(nextScale, xRatio, yRatio)
  }, [centerViewport, diagramSize.height, diagramSize.width, scale])

  useEffect(() => {
    let cancelled = false
    setSvg('')
    setError('')
    setFitMode(true)
    if (!source.trim()) return () => { cancelled = true }

    void import('mermaid').then(async ({ default: mermaid }) => {
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: 'strict',
        theme: 'neutral',
        flowchart: { htmlLabels: false, curve: 'basis', useMaxWidth: false },
        fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei UI", sans-serif',
      })
      const renderId = `application-flow-${reactId.replace(/[^a-zA-Z0-9_-]/g, '')}`
      const rendered = await mermaid.render(renderId, source)
      if (!cancelled) {
        setDiagramSize(svgSize(rendered.svg))
        setSvg(rendered.svg)
      }
    }).catch((reason: unknown) => {
      if (!cancelled) setError(reason instanceof Error ? reason.message : 'Mermaid 渲染失败')
    })
    return () => { cancelled = true }
  }, [reactId, source])

  useEffect(() => {
    if (!svg) return
    fitDiagram()
  }, [diagramSize.height, diagramSize.width, fitDiagram, svg])

  useEffect(() => {
    const viewport = viewportRef.current
    if (!viewport) return
    const observer = new ResizeObserver(() => {
      if (fitMode) fitDiagram(false)
    })
    observer.observe(viewport)
    return () => observer.disconnect()
  }, [fitDiagram, fitMode])

  function handleWheel(event: ReactWheelEvent<HTMLDivElement>) {
    if (!event.ctrlKey && !event.metaKey) return
    event.preventDefault()
    changeScale(scale * (event.deltaY > 0 ? 0.9 : 1.1))
  }

  function handlePointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    const viewport = viewportRef.current
    if (!viewport || event.button !== 0) return
    dragRef.current = {
      pointerId: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      left: viewport.scrollLeft,
      top: viewport.scrollTop,
    }
    viewport.setPointerCapture(event.pointerId)
    setDragging(true)
  }

  function handlePointerMove(event: ReactPointerEvent<HTMLDivElement>) {
    const viewport = viewportRef.current
    const drag = dragRef.current
    if (!viewport || !drag || drag.pointerId !== event.pointerId) return
    viewport.scrollLeft = drag.left - (event.clientX - drag.x)
    viewport.scrollTop = drag.top - (event.clientY - drag.y)
  }

  function stopDragging(event: ReactPointerEvent<HTMLDivElement>) {
    if (dragRef.current?.pointerId !== event.pointerId) return
    dragRef.current = null
    setDragging(false)
  }

  if (error) return <div className="application-flow-diagram-state error"><CircleAlert size={17} /><span>{error}</span></div>
  if (!svg) return <div className="application-flow-diagram-state"><LoaderCircle size={17} className="spin" /><span>正在渲染流程图</span></div>

  return <div className="mermaid-viewer">
    <div className="mermaid-viewer-toolbar" role="toolbar" aria-label="流程图缩放工具">
      <button type="button" title="缩小流程图" aria-label="缩小流程图" disabled={scale <= MIN_SCALE} onClick={() => changeScale(scale / 1.2)}><ZoomOut size={15} /></button>
      <span>{Math.round(scale * 100)}%</span>
      <button type="button" title="放大流程图" aria-label="放大流程图" disabled={scale >= MAX_SCALE} onClick={() => changeScale(scale * 1.2)}><ZoomIn size={15} /></button>
      <button type="button" title="自适应可视区域" aria-label="自适应可视区域" className={fitMode ? 'selected' : ''} onClick={() => fitDiagram()}><Maximize2 size={15} /></button>
      <button type="button" title="恢复 100%" aria-label="恢复 100%" onClick={() => { setFitMode(false); setScale(1); centerViewport(1) }}><RotateCcw size={15} /></button>
    </div>
    <div
      ref={viewportRef}
      className={`application-flow-diagram ${dragging ? 'dragging' : ''}`}
      onWheel={handleWheel}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={stopDragging}
      onPointerCancel={stopDragging}
    >
      <div
        className="application-flow-diagram-canvas"
        style={{ width: `${diagramSize.width * scale}px`, height: `${diagramSize.height * scale}px` }}
        dangerouslySetInnerHTML={{ __html: svg }}
      />
    </div>
  </div>
}
