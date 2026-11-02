import type { NextConfig } from 'next'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const apiProxyTarget = process.env.HETERO_NEXT_API_PROXY_TARGET ?? 'http://127.0.0.1:8006'

type PlatformBuild = {
  version?: string
  release?: string
  build_id?: string
  source_revision?: string
  source_revision_short?: string
  source_fingerprint_sha256?: string
  source_dirty?: boolean
  built_at?: string
}

function platformBuild(): PlatformBuild {
  try {
    return JSON.parse(readFileSync(resolve(process.cwd(), '.platform-build.json'), 'utf8')) as PlatformBuild
  } catch {
    const { version } = JSON.parse(readFileSync(resolve(process.cwd(), 'package.json'), 'utf8')) as { version: string }
    return { version, release: 'development', build_id: 'development', source_revision: 'unknown' }
  }
}

const build = platformBuild()

const nextConfig: NextConfig = {
  reactStrictMode: true,
  allowedDevOrigins: ['localhost', '127.0.0.1'],
  experimental: {
    // Raw HiTrace captures routinely exceed Next's 10 MiB proxy default. The
    // FastAPI upload endpoint enforces the same 2 GiB ceiling independently.
    proxyClientMaxBodySize: '2gb',
    // TraceStreamer has a five-minute conversion budget for first-time inputs.
    proxyTimeout: 330_000,
  },
  env: {
    NEXT_PUBLIC_HETERO_VERSION: build.version ?? 'unknown',
    NEXT_PUBLIC_HETERO_RELEASE: build.release ?? 'development',
    NEXT_PUBLIC_HETERO_BUILD_ID: build.build_id ?? 'development',
    NEXT_PUBLIC_HETERO_SOURCE_REVISION: build.source_revision ?? 'unknown',
    NEXT_PUBLIC_HETERO_SOURCE_REVISION_SHORT: build.source_revision_short ?? 'unknown',
    NEXT_PUBLIC_HETERO_SOURCE_FINGERPRINT: build.source_fingerprint_sha256 ?? '',
    NEXT_PUBLIC_HETERO_SOURCE_DIRTY: build.source_dirty ? '1' : '0',
    NEXT_PUBLIC_HETERO_BUILT_AT: build.built_at ?? '',
  },
  async headers() {
    return [
      {
        source: '/',
        headers: [
          { key: 'Cache-Control', value: 'no-store, max-age=0, must-revalidate' },
          { key: 'Pragma', value: 'no-cache' },
          { key: 'Expires', value: '0' },
        ],
      },
    ]
  },
  async rewrites() {
    // HTTP API proxy. tools/serve-platform.mjs owns the dedicated Device
    // Bridge WebSocket Upgrade because Next rewrites don't proxy raw sockets.
    return [{ source: '/v1/:path*', destination: `${apiProxyTarget}/v1/:path*` }]
  },
}

export default nextConfig
