'use client'

import { useState } from 'react'
import { ArrowRight, GitBranch, SlidersHorizontal } from 'lucide-react'
import '../cost-model-explanation.css'

const classLabel: Record<string, string> = { compute: '计算', memory: '访存', mixed: '混合' }
const coreLabel: Record<string, string> = { big: '大核', middle: '中核', small: '小核' }
export function InterferenceCalibrationTable({ model }: { model: unknown }) {
  if (!model || typeof model !== 'object') return null
  const matrix = (model as Record<string, unknown>).interference_model
  if (!matrix || typeof matrix !== 'object') return null
  const values = matrix as Record<string, unknown>
  if (!Array.isArray(values.cells) || !values.cells.length) return null
  const cells = values.cells.filter((cell): cell is Record<string, unknown> => Boolean(cell && typeof cell === 'object'))
  return <details className="pm-details"><summary>共跑干扰矩阵与测量适用域 · {cells.length} 个方向</summary><p className="pm-hint">{values.status === 'proxy_holdout_validated' ? '代理留出验证通过' : '待验证估计'}。β 表示目标任务的 CPU 膨胀比例；核组和频点外回退 alpha。</p><div className="pm-table-scroll"><table className="pm-table"><thead><tr><th>目标 ← 干扰者</th><th>核组 / 频率 MHz</th><th>CPU 膨胀</th><th>测量放置与工作集</th></tr></thead><tbody>{cells.map(cell => <tr key={String(cell.id)}><td>{classLabel[String(cell.victim_class)]} ← {classLabel[String(cell.peer_class)]}</td><td>{coreLabel[String(cell.victim_core_kind)]} {Number(cell.victim_frequency_khz) / 1000}<br />← {coreLabel[String(cell.peer_core_kind)]} {Number(cell.peer_frequency_khz) / 1000}</td><td>+{(Number(cell.beta) * 100).toFixed(1)}%</td><td>CPU {String(cell.victim_cpu)} ← {String(cell.peer_cpu)}<small>每线程 {Number(cell.working_set_bytes) / 1024 / 1024} MiB · 两任务</small></td></tr>)}</tbody></table></div></details>
}

export function ParallelInterferenceExplanation({ onPreparePlan }: { onPreparePlan: () => void }) {
  const [inflationPercent, setInflationPercent] = useState(20)
  const finish = 1 + inflationPercent / 100
  return <div className="cost-model-explanation cm-parallel-model">
    <div className="cm-intro"><div><span className="cm-kicker">第二部分 · 多任务共跑干扰</span><h4>任务分别有多快，与它们一起运行时有多快，是两次校准</h4></div></div>
    <p className="cm-reference">先用单任务模型得到每段的独立成本，再根据双方负载类型、核组、频点查找实测干扰倍率。计算密集或访存密集是实验分组依据，收益由数据决定。</p>
    <div className="ci-measurement-path"><div><strong>匹配单跑</strong><span>固定输入、工作集、核与频点</span></div><ArrowRight size={16} /><div><strong>定向共跑</strong><span>目标 A + 持续干扰者 B</span></div><ArrowRight size={16} /><div><strong>独立留出</strong><span>更换工作量，验证膨胀预测</span></div></div>
    <div className="ci-formula" role="math" aria-label="A受到B干扰的系数beta，等于A共跑CPU时间除以A单跑CPU时间减一"><span>β<sub>A ← B</sub> = </span><span className="cm-fraction"><span>A 共跑 CPU 时间</span><span>A 同条件单跑 CPU 时间</span></span><span> − 1</span></div>
    <p className="cm-reference">分别标定 A ← B 和 B ← A；两个方向可能不同。当前模型表征非负干扰，接近零的测量噪声归零，不用一个整体 wall 时间替代双方 CPU 成本。</p>
    <div className="ci-matrix-wrap"><table className="ci-matrix"><caption>有方向的负载组合矩阵 · 下表为待测项目</caption><thead><tr><th>目标 ↓ / 干扰者 →</th><th>计算代理</th><th>访存代理</th><th>混合代理</th></tr></thead><tbody>{['计算代理', '访存代理', '混合代理'].map(victim => <tr key={victim}><th>{victim}</th>{['计算代理', '访存代理', '混合代理'].map(peer => <td key={peer}>单跑 / 共跑</td>)}</tr>)}</tbody></table></div>
    <div className="cm-example"><div className="cm-example-heading"><div><SlidersHorizontal size={16} /><h4>两个 1 ms 任务，为什么不一定 1 ms 完成？</h4></div><span>示意参数 · 非实测结果</span></div><div className="cm-example-grid"><div className="cm-controls"><p>假设两个独立任务同时在不同物理核启动，单跑均为 1 ms，双方膨胀相同。这里不加入启动开销和系统服务。</p><label><span>共跑 CPU 膨胀<output>{inflationPercent}%</output></span><input type="range" aria-label="示意共跑膨胀" min={0} max={100} step={5} value={inflationPercent} onChange={event => setInflationPercent(Number(event.target.value))} /><small>可以调到 20% 或 50%，比较 1.2 ms 与 1.5 ms。</small></label></div><div className="cm-example-result" aria-live="polite"><div className="ci-tasks"><div><span>任务 A</span><i style={{ width: `${finish / 2 * 100}%` }}>1 × {finish.toFixed(2)}</i></div><div><span>任务 B</span><i style={{ width: `${finish / 2 * 100}%` }}>1 × {finish.toFixed(2)}</i></div></div><div className="cm-worked-equation"><span>并行结束时间</span><span><strong data-testid="example-parallel-time">{finish.toFixed(2)}</strong> ms</span></div><p>相对串行 2 ms 的加速比为 {(2 / finish).toFixed(2)} 倍。示例不把某一倍率绑定到计算或访存类别。</p></div></div></div>
    <div className="cm-total"><div className="cm-total-heading"><GitBranch size={17} /><h4>校准如何进入并行寻优？</h4></div><div className="cm-simulation-path"><span>阶段特征类别</span><ArrowRight size={14} /><span>活动任务组合 → 匹配实测 β</span><ArrowRight size={14} /><strong>更新剩余工作速率</strong></div><p>每次任务开始、结束或进入等待，仿真重算活动组合。ΔW = Δt / [频点性能曲线 × (1 + β)]；曲线未命中时才回退到核型 CPU 倍率与线性频率弹性，沿任务依赖、执行槽与服务通道推进至 UI 回调结束。</p></div>
    <details className="cm-detail"><summary>适用范围与缺少校准时的处理</summary><p>首版矩阵覆盖两个活动应用 CPU 任务及测量核组/频点。主线程和分支启动也占活动任务；它们没有可靠类别时，或总活动数超过 2、组合/频点未测时，回退现有 α × (N − 1) 模型，寻优结果单列匹配与回退时长。</p><p>固定工作集、设备/固件、二进制、具体物理核放置与独立运行记录随版本保存。按核组向应用阶段迁移仍需检验共享缓存、工作集与内存通道差异；缺失 PMU 的阶段不会自动标为计算密集。</p><p>校准包提供整数运算、顺序访存、交错混合三个可执行内核。CPU 时间用于干扰拟合；串行依赖、同步服务和分支启动开销在调度仿真中单独处理。</p></details>
    <div className="pm-actions"><button className="quiet-button" onClick={onPreparePlan}>配置负载组合校准<ArrowRight size={14} /></button></div>
  </div>
}
