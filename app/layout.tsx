/* oxlint-disable react/only-export-components -- Next.js layout modules export metadata by design. */
import type { Metadata } from 'next'
import type { ReactNode } from 'react'
import '@xterm/xterm/css/xterm.css'
import './globals.css'
import { BuildAssetRecovery } from './components/BuildAssetRecovery'

export const metadata: Metadata = {
  title: 'Heterogeneous Enablement Workbench',
  description: 'Local heterogeneous optimization opportunity workbench',
  icons: { icon: '/favicon.svg' },
}

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>
        <BuildAssetRecovery />
        {children}
      </body>
    </html>
  )
}
