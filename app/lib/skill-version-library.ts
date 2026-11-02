export type SkillVersionEntry = {
  version: string
  prompt_slot: string
  content_sha256: string
  prompt_mode?: 'append' | 'replace'
  content_chars?: number
  source?: string
  addendum?: string
  prompt?: string
  change_summary_zh?: string
  origin?: 'server' | 'user'
  user_version_id?: string
  server_version?: string | null
  label_zh?: string
  description_zh?: string
}

export type SkillVersionProfile = {
  profile_id: string
  label_zh: string
  description_zh: string
  created_at: string
  recommended: boolean
  parent_profile_id?: string
  profile_sha256: string
  user_skill_revision?: number
  skill_resolution_order?: string[]
  skills: Record<string, SkillVersionEntry>
}

export type SkillVersionLibrary = {
  schema_version: 'skill_version_library/v1'
  default_profile_id: string
  profiles: SkillVersionProfile[]
  selection_scope_zh: string
  user_skill_library?: {
    revision: number
    updated_at?: string | null
    skill_count: number
    resolution_order: string[]
  }
}

// v7 selects cross-page semantic code retrieval for new Runs. Older profiles
// remain selectable for rollback, while historical Runs keep frozen IDs and hashes.
const storageKey = 'heterogeneous-enable-skill-profile-v7'

export function readSkillProfileId(fallback: string): string {
  if (typeof window === 'undefined') return fallback
  try {
    return window.localStorage.getItem(storageKey)?.trim() || fallback
  } catch {
    return fallback
  }
}

export function writeSkillProfileId(profileId: string) {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(storageKey, profileId)
  } catch {
    // Browser storage is a convenience. The selected profile is also frozen
    // into each Run and reasoning artifact by the backend.
  }
}

export function skillPromptOverlay(profile: SkillVersionProfile | null | undefined, slot: string): string {
  const skill = Object.values(profile?.skills ?? {}).find((item) => item.prompt_slot === slot)
  return (skill?.addendum || skill?.prompt || '').trim()
}

export function applySkillPrompt(basePrompt: string, profile: SkillVersionProfile | null | undefined, slot: string): string {
  const skill = Object.values(profile?.skills ?? {}).find((item) => item.prompt_slot === slot)
  const material = (skill?.prompt || skill?.addendum || '').trim()
  if (!material) return basePrompt
  if (skill?.prompt_mode === 'replace') return material
  return [basePrompt.trim(), material].filter(Boolean).join('\n\n')
}

export function compactSkillProfile(profile: SkillVersionProfile | null | undefined): Record<string, unknown> | null {
  if (!profile) return null
  return {
    profile_id: profile.profile_id,
    profile_sha256: profile.profile_sha256,
    user_skill_revision: profile.user_skill_revision ?? 0,
    resolution_order: profile.skill_resolution_order ?? [],
    skills: Object.fromEntries(Object.entries(profile.skills).map(([skillId, skill]) => [skillId, {
      version: skill.version,
      content_sha256: skill.content_sha256,
      origin: skill.origin ?? 'server',
      user_version_id: skill.user_version_id ?? null,
    }])),
  }
}
