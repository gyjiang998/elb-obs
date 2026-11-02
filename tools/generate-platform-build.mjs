import { createHash, randomBytes } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import { readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const appRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const packageMetadata = JSON.parse(readFileSync(join(appRoot, 'package.json'), 'utf8'))
const sourceRoots = ['app', 'backend', 'agent-resources', 'device-bridge', 'deploy', 'tools']
const rootFiles = ['package.json', 'package-lock.json', 'next.config.ts', 'tsconfig.json']
const ignoredNames = new Set(['.DS_Store', '__pycache__', '.pytest_cache'])
const ignoredSuffixes = ['.pyc', '.pyo', '.tsbuildinfo']

function sourceFiles() {
  const files = []
  const visit = (path) => {
    const stat = statSync(path)
    if (stat.isDirectory()) {
      for (const entry of readdirSync(path).sort()) {
        if (!ignoredNames.has(entry)) visit(join(path, entry))
      }
      return
    }
    if (ignoredSuffixes.some((suffix) => path.endsWith(suffix))) return
    files.push(path)
  }
  for (const root of sourceRoots) visit(join(appRoot, root))
  for (const path of rootFiles) files.push(join(appRoot, path))
  return files.sort((left, right) => relative(appRoot, left).localeCompare(relative(appRoot, right)))
}

function fingerprint() {
  const digest = createHash('sha256')
  for (const path of sourceFiles()) {
    digest.update(relative(appRoot, path).replaceAll('\\', '/'))
    digest.update('\0')
    digest.update(readFileSync(path))
    digest.update('\0')
  }
  return digest.digest('hex')
}

function gitValue(args) {
  try {
    return execFileSync('git', args, { cwd: appRoot, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim()
  } catch {
    return ''
  }
}

const sourceFingerprint = fingerprint()
const gitRevision = process.env.HETERO_SOURCE_REVISION?.trim() || gitValue(['rev-parse', 'HEAD'])
const gitDirty = Boolean(gitValue(['status', '--porcelain', '--untracked-files=no']))
const sourceRevision = gitRevision || `src-${sourceFingerprint.slice(0, 12)}`
const builtAt = new Date().toISOString()
const buildSeed = `${packageMetadata.version}\0${sourceRevision}\0${sourceFingerprint}\0${builtAt}\0${randomBytes(8).toString('hex')}`
const buildId = `build_${createHash('sha256').update(buildSeed).digest('hex').slice(0, 20)}`
const manifest = {
  schema_version: 'platform_build/v1',
  product: 'heterogeneous-enable-web',
  version: packageMetadata.version,
  release: process.env.HETERO_PLATFORM_RELEASE?.trim() || `v${packageMetadata.version}`,
  build_id: buildId,
  source_revision: sourceRevision,
  source_revision_short: sourceRevision.slice(0, 12),
  source_fingerprint_sha256: sourceFingerprint,
  source_dirty: gitDirty,
  built_at: builtAt,
}

const serialized = `${JSON.stringify(manifest, null, 2)}\n`
writeFileSync(join(appRoot, '.platform-build.json'), serialized, 'utf8')
writeFileSync(join(appRoot, 'public', 'platform-build.json'), serialized, 'utf8')
process.stdout.write(`Platform build ${manifest.release} ${manifest.build_id} (${manifest.source_revision_short}${gitDirty ? ', dirty' : ''})\n`)
