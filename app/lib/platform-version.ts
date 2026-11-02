export type PlatformBuildInfo = {
  schema_version: 'platform_build/v1' | string
  product: string
  version: string
  release: string
  build_id: string
  source_revision: string
  source_revision_short: string
  source_fingerprint_sha256: string
  source_dirty: boolean
  built_at: string | null
}

export type PlatformVersion = {
  schema_version: 'platform_version/v1'
  build: PlatformBuildInfo
  api_runtime: {
    service: string
    started_at: string
    process_id: number
  }
  contracts: {
    agent_cli_adapter: string
    agent_cli_catalog: string
    platform_mcp: string
    mcp_tool_count: number
    mcp_facade_sha256: string
    required_reasoning_tools: string[]
  }
}

export const browserPlatformBuild: PlatformBuildInfo = {
  schema_version: 'platform_build/v1',
  product: 'heterogeneous-enable-web',
  version: process.env.NEXT_PUBLIC_HETERO_VERSION ?? 'unknown',
  release: process.env.NEXT_PUBLIC_HETERO_RELEASE ?? 'development',
  build_id: process.env.NEXT_PUBLIC_HETERO_BUILD_ID ?? 'development',
  source_revision: process.env.NEXT_PUBLIC_HETERO_SOURCE_REVISION ?? 'unknown',
  source_revision_short: process.env.NEXT_PUBLIC_HETERO_SOURCE_REVISION_SHORT ?? 'unknown',
  source_fingerprint_sha256: process.env.NEXT_PUBLIC_HETERO_SOURCE_FINGERPRINT ?? '',
  source_dirty: process.env.NEXT_PUBLIC_HETERO_SOURCE_DIRTY === '1',
  built_at: process.env.NEXT_PUBLIC_HETERO_BUILT_AT || null,
}

export function shortBuildId(value: string): string {
  return value.startsWith('build_') ? value.slice(6, 14) : value.slice(0, 12)
}

export function shortRevision(build: PlatformBuildInfo): string {
  return build.source_revision_short || build.source_revision.slice(0, 12) || 'unknown'
}
