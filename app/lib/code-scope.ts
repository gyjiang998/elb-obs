export type CandidateCodeScopeLevel = 'loop_region' | 'data_pipeline' | 'callee'

export type CandidateCodeScope = {
  scope_id: string
  level: CandidateCodeScopeLevel
  line_start: number
  line_end: number
  selection_priority: number
  signals: string[]
  description_zh: string
  symbol?: string
  resolution_status: 'observed' | 'callee_unresolved'
}

const MAX_BLOCK_LINES = 96
const COMPUTE_CALLEE = /(?:apply|blend|blur|color|compress|compute|convert|decode|encode|effect|filter|infer|kernel|matmul|mix|process|render|resize|scale|transform|convol|fft)/i
const CONTROL_KEYWORDS = new Set(['if', 'for', 'while', 'switch', 'return', 'sizeof', 'catch', 'static_cast', 'reinterpret_cast', 'const_cast', 'dynamic_cast'])

function escapeExpression(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function looksLikeFunctionDeclaration(line: string, symbol: string): boolean {
  const escaped = escapeExpression(symbol)
  const expression = new RegExp(
    `^\\s*(?:(?:inline|static|virtual|constexpr|explicit|friend|extern)\\s+)*(?:[A-Za-z_][A-Za-z0-9_:<>]*(?:\\s*[*&])?\\s+)+${escaped}\\s*\\(`,
  )
  return expression.test(line)
}

function braceDelta(line: string): number {
  let delta = 0
  let quoted = false
  let escaped = false
  for (const character of line) {
    if (quoted) {
      if (escaped) escaped = false
      else if (character === '\\') escaped = true
      else if (character === '"') quoted = false
      continue
    }
    if (character === '"') {
      quoted = true
      continue
    }
    if (character === '{') delta += 1
    if (character === '}') delta -= 1
  }
  return delta
}

function blockEnd(lines: string[], start: number): number {
  const searchEnd = Math.min(lines.length - 1, start + 2)
  let opening = -1
  for (let index = start; index <= searchEnd; index += 1) {
    if (lines[index]?.includes('{')) {
      opening = index
      break
    }
  }
  if (opening < 0) return Math.min(lines.length - 1, start + 4)

  let depth = 0
  for (let index = opening; index < Math.min(lines.length, opening + MAX_BLOCK_LINES); index += 1) {
    depth += braceDelta(lines[index])
    if (depth <= 0) return index
  }
  return Math.min(lines.length - 1, opening + MAX_BLOCK_LINES - 1)
}

function scopeSignals(lines: string[]): string[] {
  const text = lines.join('\n')
  const signals: string[] = []
  if (/\b(?:for|while)\s*\(/.test(text)) signals.push('循环')
  if (/\[[^\]]*\]|\*\s*[A-Za-z_]/.test(text)) signals.push('数组或指针访问')
  if (/\b(?:sin|cos|sqrt|exp|log|fft|fma)\s*\(/.test(text) || /[+\-*/]=|\*=|\+=/.test(text)) signals.push('算术计算')
  if (/\b(?:memcpy|memmove|memset)\s*\(/.test(text)) signals.push('批量内存操作')
  if (/\b(?:float|double|uint\d+_t|int\d+_t|size_t)\b/.test(text)) signals.push('数值或尺寸类型')
  if (/\b(?:mutex|lock_guard|shared_lock|unique_lock|SendTransactCmd|SendRequest)\b/.test(text)) signals.push('同步或外部调用边界')
  return signals
}

function scopeDescription(level: CandidateCodeScopeLevel, start: number, end: number, signals: string[], symbol?: string): string {
  const evidence = signals.length > 0 ? `可见信号：${signals.join('、')}。` : '可见源码信号有限。'
  if (level === 'callee') return `第 ${start} 行调用 ${symbol ?? '候选被调函数'}；函数体未在当前粘贴代码中展开。${evidence}`
  if (level === 'data_pipeline') return `第 ${start}-${end} 行的可见数据处理片段。${evidence}`
  return `第 ${start}-${end} 行的可见循环计算片段。${evidence}`
}

function makeScope(
  level: CandidateCodeScopeLevel,
  start: number,
  end: number,
  priority: number,
  signals: string[],
  symbol?: string,
): CandidateCodeScope {
  const resolutionStatus = level === 'callee' ? 'callee_unresolved' : 'observed'
  return {
    scope_id: `${level}_${start + 1}_${symbol ?? 'region'}`,
    level,
    line_start: start + 1,
    line_end: end + 1,
    selection_priority: priority,
    signals,
    description_zh: scopeDescription(level, start + 1, end + 1, signals, symbol),
    ...(symbol ? { symbol } : {}),
    resolution_status: resolutionStatus,
  }
}

/**
 * Produce bounded lexical hints for code-region selection. They are deliberately
 * not AST facts: a visible loop/data region can be assessed locally, while a
 * compute-looking callee stays explicitly unresolved until its body is loaded.
 */
export function candidateCodeScopes(code: string, limit = 4): CandidateCodeScope[] {
  const lines = code.split('\n')
  const candidates: CandidateCodeScope[] = []

  lines.forEach((line, index) => {
    if (/\b(?:for|while)\s*\(/.test(line)) {
      const end = blockEnd(lines, index)
      const scopeLines = lines.slice(index, end + 1)
      const signals = scopeSignals(scopeLines)
      const compactness = Math.max(0, 24 - Math.min(24, end - index))
      const priority = 100 + signals.length * 6 + compactness
      candidates.push(makeScope('loop_region', index, end, priority, signals))
    }

    if (/\b(?:memcpy|memmove|memset)\s*\(/.test(line)) {
      const end = Math.min(lines.length - 1, index + 2)
      const signals = scopeSignals(lines.slice(Math.max(0, index - 1), end + 1))
      candidates.push(makeScope('data_pipeline', Math.max(0, index - 1), end, 64 + signals.length * 4, signals))
    }

    const callMatches = line.matchAll(/\b([A-Za-z_][A-Za-z0-9_:]*)\s*\(/g)
    for (const match of callMatches) {
      const symbol = match[1]
      if (CONTROL_KEYWORDS.has(symbol) || !COMPUTE_CALLEE.test(symbol) || looksLikeFunctionDeclaration(line, symbol)) continue
      const signals = scopeSignals([line])
      candidates.push(makeScope('callee', index, index, 72 + signals.length * 3, signals, symbol))
    }
  })

  const deduplicated = new Map<string, CandidateCodeScope>()
  candidates.forEach((scope) => {
    const key = `${scope.level}:${scope.line_start}:${scope.line_end}:${scope.symbol ?? ''}`
    const current = deduplicated.get(key)
    if (!current || scope.selection_priority > current.selection_priority) deduplicated.set(key, scope)
  })
  const selected: CandidateCodeScope[] = []
  for (const scope of [...deduplicated.values()].sort((left, right) => right.selection_priority - left.selection_priority || left.line_start - right.line_start)) {
    const overlapsSelectedLoop = scope.level === 'loop_region' && selected.some((existing) => (
      existing.level === 'loop_region'
      && scope.line_start <= existing.line_end
      && existing.line_start <= scope.line_end
    ))
    if (overlapsSelectedLoop) continue
    selected.push(scope)
    if (selected.length >= Math.max(0, limit)) break
  }
  return selected
}

export function normalizeCandidateCodeScopes(value: unknown, fallbackCode: string): CandidateCodeScope[] {
  if (!Array.isArray(value)) return candidateCodeScopes(fallbackCode)
  const normalized = value.flatMap((item): CandidateCodeScope[] => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return []
    const scope = item as Record<string, unknown>
    const level = scope.level
    const lineStart = scope.line_start
    const lineEnd = scope.line_end
    if (
      (level !== 'loop_region' && level !== 'data_pipeline' && level !== 'callee')
      || typeof lineStart !== 'number'
      || typeof lineEnd !== 'number'
      || lineStart < 1
      || lineEnd < lineStart
    ) return []
    const signals = Array.isArray(scope.signals) ? scope.signals.filter((signal): signal is string => typeof signal === 'string').slice(0, 8) : []
    const symbol = typeof scope.symbol === 'string' && scope.symbol ? scope.symbol : undefined
    const resolutionStatus = scope.resolution_status === 'callee_unresolved' ? 'callee_unresolved' : 'observed'
    return [{
      scope_id: typeof scope.scope_id === 'string' && scope.scope_id ? scope.scope_id : `${level}_${lineStart}_${symbol ?? 'region'}`,
      level,
      line_start: lineStart,
      line_end: lineEnd,
      selection_priority: typeof scope.selection_priority === 'number' ? scope.selection_priority : 0,
      signals,
      description_zh: typeof scope.description_zh === 'string' && scope.description_zh
        ? scope.description_zh
        : scopeDescription(level, lineStart, lineEnd, signals, symbol),
      ...(symbol ? { symbol } : {}),
      resolution_status: resolutionStatus,
    }]
  })
  return normalized.length > 0 ? normalized.slice(0, 4) : candidateCodeScopes(fallbackCode)
}
