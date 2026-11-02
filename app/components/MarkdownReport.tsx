'use client'

import type { ReactNode } from 'react'
import { MermaidDiagram } from './MermaidDiagram'

function inlineMarkdown(value: string): ReactNode[] {
  const parts = value.split(/(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^\s)]+\))/g)
  return parts.filter(Boolean).map((part, index) => {
    if (part.startsWith('`') && part.endsWith('`')) return <code key={index}>{part.slice(1, -1)}</code>
    if (part.startsWith('**') && part.endsWith('**')) return <strong key={index}>{part.slice(2, -2)}</strong>
    const link = part.match(/^\[([^\]]+)\]\(([^\s)]+)\)$/)
    if (link) {
      const safeHref = /^(https?:\/\/|\/v1\/)/.test(link[2]) ? link[2] : ''
      return safeHref
        ? <a key={index} href={safeHref} target={safeHref.startsWith('http') ? '_blank' : undefined} rel={safeHref.startsWith('http') ? 'noreferrer' : undefined}>{link[1]}</a>
        : <span key={index}>{link[1]}（{link[2]}）</span>
    }
    return <span key={index}>{part}</span>
  })
}

function tableCells(line: string): string[] {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((item) => item.trim())
}

function isTableDivider(line: string): boolean {
  const cells = tableCells(line)
  return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell))
}

function startsBlock(line: string, next = ''): boolean {
  return /^```/.test(line)
    || /^#{1,6}\s+/.test(line)
    || /^\s*[-*+]\s+/.test(line)
    || /^\s*\d+\.\s+/.test(line)
    || /^>\s?/.test(line)
    || /^---+$/.test(line.trim())
    || (line.includes('|') && isTableDivider(next))
}

export function MarkdownReport({ source }: { source: string }) {
  const lines = source.replaceAll('\r\n', '\n').split('\n')
  const blocks: ReactNode[] = []
  let index = 0

  while (index < lines.length) {
    const line = lines[index]
    if (!line.trim()) {
      index += 1
      continue
    }
    const fence = line.match(/^```\s*([^\s]*)/)
    if (fence) {
      const language = fence[1].toLowerCase()
      const content: string[] = []
      index += 1
      while (index < lines.length && !/^```\s*$/.test(lines[index])) {
        content.push(lines[index])
        index += 1
      }
      index += index < lines.length ? 1 : 0
      blocks.push(language === 'mermaid'
        ? <div className="markdown-mermaid" key={`block-${index}`}><MermaidDiagram source={content.join('\n')} /></div>
        : <pre key={`block-${index}`}><code data-language={language || undefined}>{content.join('\n')}</code></pre>)
      continue
    }
    const heading = line.match(/^(#{1,6})\s+(.+)$/)
    if (heading) {
      const level = Math.min(6, heading[1].length)
      const children = inlineMarkdown(heading[2])
      blocks.push(level === 1 ? <h1 key={`block-${index}`}>{children}</h1>
        : level === 2 ? <h2 key={`block-${index}`}>{children}</h2>
          : level === 3 ? <h3 key={`block-${index}`}>{children}</h3>
            : level === 4 ? <h4 key={`block-${index}`}>{children}</h4>
              : level === 5 ? <h5 key={`block-${index}`}>{children}</h5>
                : <h6 key={`block-${index}`}>{children}</h6>)
      index += 1
      continue
    }
    if (line.includes('|') && index + 1 < lines.length && isTableDivider(lines[index + 1])) {
      const headers = tableCells(line)
      const rows: string[][] = []
      index += 2
      while (index < lines.length && lines[index].includes('|') && lines[index].trim()) {
        rows.push(tableCells(lines[index]))
        index += 1
      }
      blocks.push(<div className="markdown-table-shell" key={`block-${index}`}><table><thead><tr>{headers.map((cell, cellIndex) => <th key={cellIndex}>{inlineMarkdown(cell)}</th>)}</tr></thead><tbody>{rows.map((row, rowIndex) => <tr key={rowIndex}>{headers.map((_, cellIndex) => <td key={cellIndex}>{inlineMarkdown(row[cellIndex] ?? '')}</td>)}</tr>)}</tbody></table></div>)
      continue
    }
    const unordered = line.match(/^\s*[-*+]\s+(.+)$/)
    if (unordered) {
      const items: string[] = []
      while (index < lines.length) {
        const match = lines[index].match(/^\s*[-*+]\s+(.+)$/)
        if (!match) break
        items.push(match[1])
        index += 1
      }
      blocks.push(<ul key={`block-${index}`}>{items.map((item, itemIndex) => <li key={itemIndex}>{inlineMarkdown(item)}</li>)}</ul>)
      continue
    }
    const ordered = line.match(/^\s*\d+\.\s+(.+)$/)
    if (ordered) {
      const items: string[] = []
      while (index < lines.length) {
        const match = lines[index].match(/^\s*\d+\.\s+(.+)$/)
        if (!match) break
        items.push(match[1])
        index += 1
      }
      blocks.push(<ol key={`block-${index}`}>{items.map((item, itemIndex) => <li key={itemIndex}>{inlineMarkdown(item)}</li>)}</ol>)
      continue
    }
    if (/^>\s?/.test(line)) {
      const quote: string[] = []
      while (index < lines.length && /^>\s?/.test(lines[index])) {
        quote.push(lines[index].replace(/^>\s?/, ''))
        index += 1
      }
      blocks.push(<blockquote key={`block-${index}`}>{inlineMarkdown(quote.join(' '))}</blockquote>)
      continue
    }
    if (/^---+$/.test(line.trim())) {
      blocks.push(<hr key={`block-${index}`} />)
      index += 1
      continue
    }
    const paragraph = [line.trim()]
    index += 1
    while (index < lines.length && lines[index].trim() && !startsBlock(lines[index], lines[index + 1] ?? '')) {
      paragraph.push(lines[index].trim())
      index += 1
    }
    blocks.push(<p key={`block-${index}`}>{inlineMarkdown(paragraph.join(' '))}</p>)
  }

  return <article className="markdown-report">{blocks}</article>
}
