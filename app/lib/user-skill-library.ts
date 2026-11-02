export type ManagedSkillVersion = {
  version_id: string
  version: string
  origin: 'server' | 'user'
  content_sha256: string
  content_chars?: number
  active: boolean
  created_at?: string | null
  created_by?: string
  change_summary_zh?: string
}

export type ManagedSkillSummary = {
  skill_id: string
  label_zh: string
  description_zh: string
  prompt_slot: string
  effective_origin: 'server' | 'user' | 'inactive_user'
  effective_version: string | null
  effective_version_id: string | null
  active_user_version_id: string | null
  server_version: string | null
  has_server_version: boolean
  user_version_count: number
  versions: ManagedSkillVersion[]
}

export type UserSkillManagementLibrary = {
  schema_version: 'skill_management_library/v1'
  profile_id: string
  profile_sha256: string
  user_skill_revision: number
  resolution_order: string[]
  skills: ManagedSkillSummary[]
}

export type ManagedSkillDetail = {
  schema_version: 'skill_management_detail/v1'
  skill: {
    skill_id: string
    version?: string
    version_id?: string
    label_zh?: string
    description_zh?: string
    prompt_slot?: string
    prompt_mode?: 'append' | 'replace'
    origin?: 'server' | 'user'
    content?: string
    prompt?: string
    content_sha256?: string
    content_chars?: number
    created_at?: string
    created_by?: string
    change_summary_zh?: string
  }
  effective: boolean
  server_baseline: { version?: string; content_sha256?: string } | null
}

export type SkillLibraryPresentation = {
  skill_id: string
  label_zh: string
  description_zh: string
  target_view?: string
  prompt_slot?: string
  fallback_content?: string
  execution?: string
  input?: string
  output?: string
  boundary?: string
}
