'use client'

import { useState } from 'react'
import { Activity, Gauge } from 'lucide-react'
import '../cost-model-explanation.css'

const chart = { left: 66, right: 574, top: 24, bottom: 220 }
const intensityMinimum = .25
const intensityMaximum = 16
const performanceMinimum = 5
const performanceMaximum = 160

const peakAtMaximumFrequency = 120
const memoryBandwidth = 20

function x(value: number) {
  const minimum = Math.log2(intensityMinimum)
  const maximum = Math.log2(intensityMaximum)
  return chart.left + (Math.log2(Math.max(value, intensityMinimum)) - minimum) / (maximum - minimum) * (chart.right - chart.left)
}

function y(value: number) {
  const minimum = Math.log2(performanceMinimum)
  const maximum = Math.log2(performanceMaximum)
  return chart.bottom - (Math.log2(Math.max(Math.min(value, performanceMaximum), performanceMinimum)) - minimum) / (maximum - minimum) * (chart.bottom - chart.top)
}

export function RooflineFrequencyExplorer() {
  const [frequencyPercent, setFrequencyPercent] = useState(80)
  const [workloadIntensityExponent, setWorkloadIntensityExponent] = useState(2)
  const workloadIntensity = 2 ** workloadIntensityExponent
  const workloadIntensityLabel = workloadIntensity < 1 ? workloadIntensity.toFixed(2) : workloadIntensity.toFixed(1)
  const computeCeiling = peakAtMaximumFrequency * frequencyPercent / 100
  const bandwidthCeiling = memoryBandwidth * workloadIntensity
  const attainablePerformance = Math.min(computeCeiling, bandwidthCeiling)
  const ridgeIntensity = computeCeiling / memoryBandwidth
  const frequencyKnee = bandwidthCeiling / peakAtMaximumFrequency * 100
  const computeBound = computeCeiling <= bandwidthCeiling
  const stateLabel = computeBound ? '计算受限' : '带宽受限'
  const stateClass = computeBound ? 'compute' : 'memory'
  const ridgeX = x(ridgeIntensity)
  const workloadX = x(workloadIntensity)
  const workloadY = y(attainablePerformance)
  const workloadLabelAtRight = workloadX > chart.right - 90
  const currentCeilingY = y(computeCeiling)
  const currentNearMinimumReference = Math.abs(currentCeilingY - y(peakAtMaximumFrequency * .2)) < 13
  const currentNearMaximumReference = Math.abs(currentCeilingY - y(peakAtMaximumFrequency)) < 13
  const memoryRegionMidIntensity = Math.sqrt(intensityMinimum * ridgeIntensity)
  const memoryRegionLabelX = x(memoryRegionMidIntensity)
  const memoryRegionLabelY = (y(memoryBandwidth * memoryRegionMidIntensity) + chart.bottom) / 2 + 4
  const computeRegionLabelX = (ridgeX + chart.right) / 2
  const computeRegionLabelY = (currentCeilingY + chart.bottom) / 2 + 4
  const workloadLabelX = workloadX + (workloadLabelAtRight ? -10 : 10)
  const defaultWorkloadLabelY = workloadY + (workloadY > chart.bottom - 24 ? -11 : 18)
  const workloadLabelCrowdsMemoryRegion = !computeBound
    && Math.abs(workloadLabelX - memoryRegionLabelX) < 85
    && Math.abs(defaultWorkloadLabelY - memoryRegionLabelY) < 20
  const workloadLabelY = workloadLabelCrowdsMemoryRegion ? workloadY - 11 : defaultWorkloadLabelY
  const memoryBoundRegion = `${chart.left},${chart.bottom} ${chart.left},${y(memoryBandwidth * intensityMinimum)} ${ridgeX},${currentCeilingY} ${ridgeX},${chart.bottom}`
  const computeBoundRegion = `${ridgeX},${chart.bottom} ${ridgeX},${currentCeilingY} ${chart.right},${currentCeilingY} ${chart.right},${chart.bottom}`
  const frequencyBoundaryLabel = frequencyKnee < 20
    ? '低于图示最低 OPP；当前频段始终带宽受限'
    : frequencyKnee > 100
      ? '高于最高 OPP；当前频段始终计算受限'
      : `约 ${frequencyKnee.toFixed(0)}% OPP`
  const currentRoof = `M ${x(intensityMinimum)} ${y(memoryBandwidth * intensityMinimum)} L ${ridgeX} ${y(computeCeiling)} L ${x(intensityMaximum)} ${y(computeCeiling)}`

  return <section className="cm-roofline-explorer" aria-labelledby="roofline-title">
    <div className="cm-roofline-heading">
      <div><Activity size={17} /><div><span>经典 Roofline · 对数坐标示意</span><h5 id="roofline-title">频率与计算强度共同决定负载所处区间</h5></div></div>
      <output className={stateClass} aria-live="polite">{stateLabel}</output>
    </div>

    <div className="cm-roofline-layout">
      <div className="cm-roofline-chart">
        <svg viewBox="0 0 610 270" role="img" aria-label={`Roofline 图。当前频率为最高频率的 ${frequencyPercent}%，负载计算强度为 ${workloadIntensityLabel} FLOP/Byte，可达性能 ${attainablePerformance.toFixed(0)} GFLOP/s，处于${stateLabel}。当前交点竖线左侧为 Memory-bound，右侧为 Compute-bound。`}>
          <polygon className="cm-roofline-region memory" points={memoryBoundRegion} />
          <polygon className="cm-roofline-region compute" points={computeBoundRegion} />
          {[5, 10, 20, 40, 80, 160].map(value => <g key={`y-${value}`}>
            <line className="cm-roofline-grid" x1={chart.left} y1={y(value)} x2={chart.right} y2={y(value)} />
            <text className="cm-roofline-label" x={chart.left - 10} y={y(value) + 4} textAnchor="end">{value}</text>
          </g>)}
          {[.25, .5, 1, 2, 4, 8, 16].map(value => <g key={`x-${value}`}>
            <line className="cm-roofline-grid vertical" x1={x(value)} y1={chart.top} x2={x(value)} y2={chart.bottom} />
            <text className="cm-roofline-label" x={x(value)} y={chart.bottom + 18} textAnchor="middle">{value}</text>
          </g>)}
          <line className="cm-roofline-axis" x1={chart.left} y1={chart.bottom} x2={chart.right} y2={chart.bottom} />
          <line className="cm-roofline-axis" x1={chart.left} y1={chart.bottom} x2={chart.left} y2={chart.top} />

          <line className="cm-roofline-reference min" x1={chart.left} y1={y(peakAtMaximumFrequency * .2)} x2={chart.right} y2={y(peakAtMaximumFrequency * .2)} />
          <line className="cm-roofline-reference max" x1={chart.left} y1={y(peakAtMaximumFrequency)} x2={chart.right} y2={y(peakAtMaximumFrequency)} />
          {!currentNearMaximumReference && <text className="cm-roofline-reference-label" x={chart.right - 4} y={y(peakAtMaximumFrequency) - 7} textAnchor="end">100% OPP 计算上限</text>}
          {!currentNearMinimumReference && <text className="cm-roofline-reference-label" x={chart.right - 4} y={y(peakAtMaximumFrequency * .2) - 7} textAnchor="end">20% OPP 计算上限</text>}

          <line className="cm-roofline-bandwidth" x1={x(intensityMinimum)} y1={y(memoryBandwidth * intensityMinimum)} x2={x(performanceMaximum / memoryBandwidth)} y2={y(performanceMaximum)} />
          <text className="cm-roofline-bandwidth-label" x={x(1.25)} y={y(25) - 8} transform={`rotate(-34 ${x(1.25)} ${y(25) - 8})`}>带宽上限 P = BW × I</text>
          <path className={`cm-roofline-current ${stateClass}`} d={currentRoof} />
          <text className={`cm-roofline-current-label ${stateClass}`} x={chart.right - 4} y={currentCeilingY - 7} textAnchor="end">Maximum attainable performance</text>
          <line className="cm-roofline-boundary" x1={ridgeX} y1={currentCeilingY} x2={ridgeX} y2={chart.bottom} />
          <text className="cm-roofline-region-label memory" x={memoryRegionLabelX} y={memoryRegionLabelY} textAnchor="middle">Memory-bound</text>
          <text className="cm-roofline-region-label compute" x={computeRegionLabelX} y={computeRegionLabelY} textAnchor="middle">Compute-bound</text>

          <line className="cm-roofline-workload-guide" x1={workloadX} y1={workloadY} x2={workloadX} y2={chart.bottom} />
          <circle className={`cm-roofline-workload ${stateClass}`} cx={workloadX} cy={workloadY} r="7" />
          <text className="cm-roofline-workload-label" x={workloadLabelX} y={workloadLabelY} textAnchor={workloadLabelAtRight ? 'end' : 'start'}>负载</text>

          <text className="cm-roofline-axis-title y" x="16" y="128" transform="rotate(-90 16 128)" textAnchor="middle">Attainable performance (GFLOP/s)</text>
          <text className="cm-roofline-axis-title" x={(chart.left + chart.right) / 2} y="262" textAnchor="middle">Arithmetic intensity (FLOP/Byte)</text>
        </svg>
      </div>

      <div className="cm-roofline-panel">
        <div className="cm-roofline-control">
          <div className="cm-roofline-frequency"><Gauge size={16} /><span>当前核频</span><strong>{frequencyPercent}% OPP</strong></div>
          <input aria-label="Roofline 示例相对运行频率" type="range" min={20} max={100} step={1} value={frequencyPercent} onChange={event => setFrequencyPercent(Number(event.target.value))} />
        </div>
        <div className="cm-roofline-control">
          <div className="cm-roofline-frequency"><Activity size={16} /><span>负载计算强度</span><strong>{workloadIntensityLabel} FLOP/Byte</strong></div>
          <input aria-label="Roofline 示例负载计算强度" aria-valuetext={`${workloadIntensityLabel} FLOP/Byte`} type="range" min={-2} max={4} step={.25} value={workloadIntensityExponent} onChange={event => setWorkloadIntensityExponent(Number(event.target.value))} />
        </div>
        <dl>
          <div><dt>最高 OPP 算力 P<sub>peak,max</sub></dt><dd>{peakAtMaximumFrequency} GFLOP/s</dd></div>
          <div><dt>持续内存带宽 BW</dt><dd>{memoryBandwidth} GB/s</dd></div>
          <div><dt>计算上限 P<sub>peak</sub>(f)</dt><dd>{computeCeiling.toFixed(0)} GFLOP/s</dd></div>
          <div><dt>该负载的带宽上限 BW × I</dt><dd>{bandwidthCeiling.toFixed(1)} GFLOP/s</dd></div>
          <div><dt>可达性能 min(P<sub>peak</sub>, BW × I)</dt><dd>{attainablePerformance.toFixed(0)} GFLOP/s</dd></div>
        </dl>
        <div className={`cm-roofline-state ${stateClass}`}>
          <strong>{stateLabel}</strong>
          <p>{computeBound
            ? '当前负载点受计算屋顶限制；提高频率会近似线性提高性能。'
            : '当前负载点受带宽屋顶限制；继续提高频率，性能收益趋于平缓。'}</p>
        </div>
        <p className="cm-roofline-threshold">计算与带宽边界：<strong>{frequencyBoundaryLabel}</strong>。当前频率下，两类上限的交点位于 <strong>{ridgeIntensity.toFixed(1)} FLOP/Byte</strong>。</p>
      </div>
    </div>

    <div className="cm-roofline-equation" role="math" aria-label="可达性能等于当前频率下的峰值算力与带宽乘计算强度中的较小值。">
      <span>P<sub>attainable</sub>(f, I)</span><b>=</b><span>min</span><i>(</i><span>P<sub>peak,max</sub> · f/f<sub>max</sub></span><b>,</b><span>BW · I</span><i>)</i>
    </div>
    <p className="cm-roofline-caption">图中数值用于解释模型；真机校准后由所选核组、OPP 的源级计算吞吐和大工作集带宽实测替换。对同一负载，调频时计算强度保持不变；计算强度滑块用于观察不同负载在屋顶下的位置。BuildItem 以整数、分支和访存为主，应用阶段应结合 Instruction/Byte 与已有 PMU 特征分型；本轮真机校准尚未生成 Instruction Roofline，不能把整数代理误写成 FLOP Roofline。</p>
  </section>
}
