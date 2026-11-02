'use client'

import { useState, type ReactNode } from 'react'
import { ArrowRight, GitBranch, SlidersHorizontal } from 'lucide-react'
import { RooflineFrequencyExplorer } from './RooflineFrequencyExplorer'
import '../cost-model-explanation.css'

function Fraction({ top, bottom }: { top: ReactNode; bottom: ReactNode }) {
  return <span className="cm-fraction"><span>{top}</span><span>{bottom}</span></span>
}

const curves = {
  compute: {
    label: '计算密集', english: 'compute', knee: .9, above: .2, below: 1,
    note: '拐点高，较宽频段内都对核频敏感',
  },
  mixed: {
    label: '混合型', english: 'mixed', knee: .72, above: .12, below: .95,
    note: '拐点居中，计算与带宽共同限制',
  },
  memory: {
    label: '访存密集', english: 'memory', knee: .56, above: .05, below: .9,
    note: '拐点低，越过后升频收益快速收敛',
  },
} as const
type WorkloadClass = keyof typeof curves

const workloadOrder: WorkloadClass[] = ['compute', 'mixed', 'memory']
const plot = { left: 42, right: 390, top: 16, bottom: 132 }

function frequencyFactor(frequency: number, curve: typeof curves[WorkloadClass]) {
  if (frequency >= curve.knee) return (1 / frequency) ** curve.above
  return (1 / curve.knee) ** curve.above * (curve.knee / frequency) ** curve.below
}

function plotX(frequency: number) {
  return plot.left + ((frequency - .5) / .5) * (plot.right - plot.left)
}

function plotY(factor: number) {
  const normalized = Math.max(0, Math.min(1, (factor - 1) / 1.05))
  return plot.bottom - normalized * (plot.bottom - plot.top)
}

function curvePath(curve: typeof curves[WorkloadClass]) {
  return Array.from({ length: 26 }, (_, index) => {
    const frequency = .5 + index * .02
    return `${index ? 'L' : 'M'} ${plotX(frequency).toFixed(1)} ${plotY(frequencyFactor(frequency, curve)).toFixed(1)}`
  }).join(' ')
}

export function ParallelCostModelExplanation() {
  const [frequencyPercent, setFrequencyPercent] = useState(80)
  const [workloadClass, setWorkloadClass] = useState<WorkloadClass>('memory')
  const referenceOnCpuMs = 2
  const observedOffCpuMs = .8
  const targetToReferenceCurveRatio = 1.25
  const curve = curves[workloadClass]
  const frequency = frequencyPercent / 100
  const frequencyMultiplier = frequencyFactor(frequency, curve)
  const modeledOnCpuMs = referenceOnCpuMs * targetToReferenceCurveRatio * frequencyMultiplier
  const observedWallMs = referenceOnCpuMs + observedOffCpuMs
  const currentX = plotX(frequency)
  const stages = [
    { label: 'Trace On-CPU', value: referenceOnCpuMs, tone: 'reference' },
    { label: 'Trace Off-CPU', value: observedOffCpuMs, tone: 'offcpu' },
    { label: 'Trace Wall', value: observedWallMs, tone: 'total' },
    { label: '建模后 On-CPU', value: modeledOnCpuMs, tone: 'frequency' },
  ]

  return <div className="cost-model-explanation cm-single-model">
    <div className="cm-intro">
      <div><span className="cm-kicker">第一部分 · 单任务核频成本</span><h4>保留 Trace 的等待证据，只缩放真正执行的 CPU 工作</h4></div>
      <span className="cm-unit">时间单位：ms</span>
    </div>

    <div className="cm-equation cm-wall-equation" role="math" aria-label="Trace 观测墙钟账目等于观测 On-CPU 加观测 Off-CPU。">
      <div className="cm-term cm-result"><span className="cm-symbol">T<sub>wall,obs</sub></span><span>Trace 墙钟账目</span></div><span className="cm-operator">=</span>
      <div className="cm-term frequency"><span className="cm-symbol">T<sub>onCPU,obs</sub></span><span>观测执行</span></div><span className="cm-operator">+</span>
      <div className="cm-term offcpu"><span className="cm-symbol">T<sub>offCPU,obs</sub></span><span>观测等待账目</span></div>
    </div>
    <div className="cm-oncpu-formula" role="math" aria-label="预测 On-CPU 时间等于观测 On-CPU 时间乘目标核组目标频点与参考条件下的实测单位工作量时延之比。">
      <span>T<sub>onCPU,pred</sub></span><b>=</b><span>T<sub>onCPU,obs</sub></span><b>×</b><Fraction top={<>L<sub>k,class</sub>(f)</>} bottom={<>L<sub>ref,class</sub>(f<sub>ref</sub>)</>} />
    </div>
    <p className="cm-reference"><strong>L<sub>k,class</sub>(f)</strong> 是核组 k 上该任务类型在频率 f 下实测的单位工作量时延，由最高 OPP 的绝对锚点和分段响应共同组成。目标与参考曲线的比值已经同时表达核型和频点影响，不再叠乘核型 CPU 倍率或旧线性弹性。</p>

    <div className="cm-factor-grid">
      <article className="offcpu">
        <div className="cm-factor-heading"><span>保留</span><strong>Off-CPU 状态与依赖</strong></div>
        <div className="cm-offcpu-ledger"><span>Runnable</span><span>Interrupted（Runnable 子集）</span><span>非 Binder Delay</span><span>Unknown</span><span>Binder 因果链</span></div>
        <p>非 Binder 等待以实测 delay 节点保留；同步 Binder blocked 由 send → service → reply → wake 依赖链表达，不再重复加一段固定等待。Off-CPU 汇总保留用于观测账目。</p>
      </article>
      <article className="core">
        <div className="cm-factor-heading"><span>兼容回退</span><strong>核型 CPU 倍率 R<sub>k</sub></strong></div>
        <div className="cm-factor-formula" role="math" aria-label="核型倍率等于相同代理工作在目标核组与参考核组最高频点下的 On-CPU 时间之比。"><span className="cm-symbol">R<sub>k</sub> = </span><Fraction top="目标核组 On-CPU" bottom="参考核组 On-CPU" /></div>
        <p>仅在目标核组或任务类型缺少完整实测曲线时使用；固定工作量和物理落核，在各核组最高 OPP 配对实测。</p>
      </article>
      <article className="frequency">
        <div className="cm-factor-heading"><span>主校准</span><strong>任务类型频点性能曲线 L</strong></div>
        <div className="cm-piecewise" role="math" aria-label="频率曲线在拐点以上和以下使用不同指数，并在拐点连续。">
          <span>f ≥ f<sub>knee</sub></span><b>(f<sub>max</sub>/f)<sup>e<sub>hi</sub></sup></b>
          <span>f &lt; f<sub>knee</sub></span><b>(f<sub>max</sub>/f<sub>knee</sub>)<sup>e<sub>hi</sub></sup> · (f<sub>knee</sub>/f)<sup>e<sub>lo</sub></sup></b>
        </div>
        <p>L 以每个核组、任务类型在最高 OPP 的绝对单位时延为锚，G 表示同一曲线内的相对频率响应。f 低于 f<sub>knee</sub> 时通常仍受计算上限约束；超过阈值后带宽逐渐成为上限，继续升频的收益趋缓，因此低频段用较大的 e<sub>lo</sub>，高频段用较小的 e<sub>hi</sub>。</p>
      </article>
    </div>

    <RooflineFrequencyExplorer />

    <div className="cm-curve-lab">
      <div className="cm-curve-controls" role="tablist" aria-label="示例负载类型">
        {workloadOrder.map(kind => <button type="button" role="tab" aria-selected={workloadClass === kind} key={kind} className={`${kind} ${workloadClass === kind ? 'selected' : ''}`} onClick={() => setWorkloadClass(kind)}>
          <span className="cm-curve-swatch" /><span><strong>{curves[kind].label}</strong><small>{curves[kind].english} · 拐点 {Math.round(curves[kind].knee * 100)}%</small></span>
        </button>)}
      </div>
      <div className="cm-curve-plot">
        <div className="cm-curve-heading"><div><SlidersHorizontal size={16} /><strong>任务类型频点性能曲线</strong></div><output>{frequencyPercent}% OPP</output></div>
        <svg viewBox="0 0 410 158" role="img" aria-label="计算密集、混合型和访存密集任务的示例分段频率响应曲线。纵轴是相对最高频点的 On-CPU 时延，计算密集型拐点高，访存密集型拐点低。">
          <rect className="cm-knee-region sensitive" x={plot.left} y={plot.top} width={plotX(curve.knee) - plot.left} height={plot.bottom - plot.top} />
          <rect className="cm-knee-region saturated" x={plotX(curve.knee)} y={plot.top} width={plot.right - plotX(curve.knee)} height={plot.bottom - plot.top} />
          <text className="cm-knee-region-label sensitive" x={plot.left + 5} y={plot.top - 5}>频率敏感区 · 升频收益高</text>
          <text className="cm-knee-region-label saturated" x={plot.right - 5} y={plot.top - 5} textAnchor="end">带宽受限区 · 收益趋缓</text>
          <line className="cm-chart-axis" x1={plot.left} y1={plot.bottom} x2={plot.right} y2={plot.bottom} />
          <line className="cm-chart-grid" x1={plot.left} y1={plotY(1.5)} x2={plot.right} y2={plotY(1.5)} />
          <text className="cm-chart-label" x={plot.left - 5} y={plotY(1) + 4} textAnchor="end">1.0×</text>
          <text className="cm-chart-label" x={plot.left - 5} y={plotY(1.5) + 4} textAnchor="end">1.5×</text>
          <text className="cm-chart-label" x={plot.left} y="151" textAnchor="middle">50%</text>
          <text className="cm-chart-label" x={plot.right} y="151" textAnchor="middle">100%</text>
          <text className="cm-chart-label cm-curve-y-title" x="11" y={(plot.top + plot.bottom) / 2} textAnchor="middle" transform={`rotate(-90 11 ${(plot.top + plot.bottom) / 2})`}>相对 On-CPU 时延 G（越低越好）</text>
          {workloadOrder.map(kind => <path key={kind} className={`cm-curve-line ${kind} ${workloadClass === kind ? 'selected' : ''}`} d={curvePath(curves[kind])} />)}
          <line className="cm-knee-frequency" x1={plotX(curve.knee)} y1={plot.top} x2={plotX(curve.knee)} y2={plot.bottom} />
          <text className="cm-knee-frequency-label" x={plotX(curve.knee) - 4} y={plot.top + 9} textAnchor="end">f_knee</text>
          <line className="cm-current-frequency" x1={currentX} y1={plot.top} x2={currentX} y2={plot.bottom} />
          {workloadOrder.map(kind => <circle key={kind} className={`cm-curve-point ${kind} ${workloadClass === kind ? 'selected' : ''}`} cx={currentX} cy={plotY(frequencyFactor(frequency, curves[kind]))} r={workloadClass === kind ? 4.5 : 3} />)}
        </svg>
        <label className="cm-frequency-slider"><span>最低 OPP</span><input aria-label="示例相对运行频率" type="range" min={50} max={100} step={2} value={frequencyPercent} onChange={event => setFrequencyPercent(Number(event.target.value))} /><span>最高 OPP</span></label>
        <p><strong>{curve.label}：</strong>{curve.note}。纵轴 G 是相对最高 OPP 的 On-CPU 时延倍率，数值越低越好；当前 G = {frequencyMultiplier.toFixed(2)}。</p>
      </div>
    </div>

    <div className="cm-calibration-flow" aria-label="频率模型校准和应用路径">
      <div><strong>微基准实测</strong><span>每核组 × 每类别：5 个训练 OPP + 1 个独立留出 OPP</span></div><ArrowRight size={14} />
      <div><strong>分段拟合与验证</strong><span>训练点识别 knee、e<sub>hi</sub>、e<sub>lo</sub>；留出点只检验误差</span></div><ArrowRight size={14} />
      <div><strong>真实阶段分型</strong><span>Trace + PMU 识别 compute / mixed / memory</span></div><ArrowRight size={14} />
      <div><strong>选取对应曲线</strong><span>曲线缺失或越界时回退核型倍率与线性弹性</span></div>
    </div>

    <div className="cm-example">
      <div className="cm-example-heading"><div><SlidersHorizontal size={16} /><h4>Trace 观测账目与当前 On-CPU 预测</h4></div><span>原理演示 · 非校准结果</span></div>
      <div className="cm-example-grid">
        <div className="cm-controls"><p>Trace 观测 On-CPU 2 ms、Off-CPU 0.8 ms，观测 Wall 为 2.8 ms；当前选择{curve.label}曲线计算新的 On-CPU 成本。</p><div className="cm-accounting-rule"><span>核频模型输出</span><strong>On-CPU {modeledOnCpuMs.toFixed(2)} ms</strong><span>仅作观测汇总</span><strong>Off-CPU 0.80 ms</strong></div></div>
        <div className="cm-example-result" aria-live="polite"><div className="cm-bars" aria-label="Trace 观测账目与建模后 On-CPU">{stages.map(stage => <div className="cm-bar-row" key={stage.label}><span>{stage.label}</span><div className="cm-bar-track"><div className={`cm-bar ${stage.tone}`} style={{ width: `${Math.min(100, stage.value / 5 * 100)}%` }} /></div><strong>{stage.value.toFixed(2)} <small>ms</small></strong></div>)}</div><div className="cm-worked-equation"><span>Trace：2.0 + 0.8</span><span>= <strong data-testid="example-wall-time">{observedWallMs.toFixed(2)}</strong> ms</span><span>预测 On-CPU：2 × 1.25 × {frequencyMultiplier.toFixed(2)} = <strong data-testid="example-cpu-time">{modeledOnCpuMs.toFixed(2)}</strong> ms</span></div></div>
      </div>
    </div>

    <div className="cm-total">
      <div className="cm-total-heading"><GitBranch size={17} /><h4>从单阶段账目到回调完成时间</h4></div>
      <div className="cm-simulation-path"><span>建模 On-CPU</span><span>实测非 Binder Delay</span><span>Binder / 服务依赖链</span><strong>DAG makespan</strong></div>
      <p>上面的加法只是 Trace 观测账目恒等式。预测时保留非 Binder delay 节点的实测时长，同步 Binder 等待沿 send → service → reply → wake 因果链推进；阶段、分支和整个 UI 回调取 DAG 最晚完成时间。<code>app_off_cpu_observed_ms</code> 只用于展示观测账目，不能直接加到预测 On-CPU 上。共跑干扰只作用于活动 On-CPU 节点。</p>
    </div>
  </div>
}
