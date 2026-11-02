import type { JsonRecord } from './types'

export const targetPlatformProfileStorageKey = 'heterogeneous-enable-target-platform-profile-v1'

export type TargetPlatformProfile = {
  schema_version: 'target_platform_profile/v1'
  profile_source: 'user_selected'
  capability_status: 'not_device_probe'
  chipset: {
    family_id: string
    family_label: string
    model_id: string
    model_label: string
  }
  operating_system: {
    family_id: string
    family_label: string
    version_id: string
    version_label: string
  }
}

type ChipsetOption = {
  id: string
  label: string
  familyId: string
  familyLabel: string
}

type OperatingSystemOption = {
  id: string
  label: string
  familyId: string
  familyLabel: string
}

export const chipsetOptions: ChipsetOption[] = [
  { id: 'kirin_unspecified', label: '麒麟 SoC（型号待确认）', familyId: 'kirin', familyLabel: '麒麟 SoC' },
  { id: 'kirin_9000', label: '麒麟 9000', familyId: 'kirin', familyLabel: '麒麟 SoC' },
  { id: 'kirin_9000s', label: '麒麟 9000S', familyId: 'kirin', familyLabel: '麒麟 SoC' },
  { id: 'kirin_9010', label: '麒麟 9010', familyId: 'kirin', familyLabel: '麒麟 SoC' },
  { id: 'kirin_9020', label: '麒麟 9020', familyId: 'kirin', familyLabel: '麒麟 SoC' },
  { id: 'kirin_other', label: '其他麒麟 SoC', familyId: 'kirin', familyLabel: '麒麟 SoC' },
  { id: 'other_unspecified', label: '其他设备（待确认）', familyId: 'other', familyLabel: '其他设备' },
]

export const operatingSystemOptions: OperatingSystemOption[] = [
  { id: 'harmonyos_unspecified', label: '鸿蒙操作系统（版本待确认）', familyId: 'harmonyos', familyLabel: '鸿蒙操作系统' },
  { id: 'harmonyos_4', label: 'HarmonyOS 4.x', familyId: 'harmonyos', familyLabel: '鸿蒙操作系统' },
  { id: 'harmonyos_5', label: 'HarmonyOS 5.x / HarmonyOS NEXT', familyId: 'harmonyos', familyLabel: '鸿蒙操作系统' },
  { id: 'openharmony_4', label: 'OpenHarmony 4.x', familyId: 'openharmony', familyLabel: 'OpenHarmony' },
  { id: 'openharmony_5', label: 'OpenHarmony 5.x', familyId: 'openharmony', familyLabel: 'OpenHarmony' },
  { id: 'other_os_unspecified', label: '其他系统（待确认）', familyId: 'other', familyLabel: '其他系统' },
]

function asRecord(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {}
}

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

export function createTargetPlatformProfile(
  chipsetModelId = 'kirin_unspecified',
  operatingSystemVersionId = 'harmonyos_unspecified',
): TargetPlatformProfile {
  const chipset = chipsetOptions.find((item) => item.id === chipsetModelId) ?? chipsetOptions[0]
  const operatingSystem = operatingSystemOptions.find((item) => item.id === operatingSystemVersionId) ?? operatingSystemOptions[0]
  return {
    schema_version: 'target_platform_profile/v1',
    profile_source: 'user_selected',
    capability_status: 'not_device_probe',
    chipset: {
      family_id: chipset.familyId,
      family_label: chipset.familyLabel,
      model_id: chipset.id,
      model_label: chipset.label,
    },
    operating_system: {
      family_id: operatingSystem.familyId,
      family_label: operatingSystem.familyLabel,
      version_id: operatingSystem.id,
      version_label: operatingSystem.label,
    },
  }
}

export function normalizeTargetPlatformProfile(value: unknown): TargetPlatformProfile {
  const profile = asRecord(value)
  const chipset = asRecord(profile.chipset)
  const operatingSystem = asRecord(profile.operating_system)
  return createTargetPlatformProfile(
    text(chipset.model_id) || 'kirin_unspecified',
    text(operatingSystem.version_id) || 'harmonyos_unspecified',
  )
}

export function targetPlatformProfileLabel(profile: TargetPlatformProfile): string {
  return `${profile.chipset.model_label} · ${profile.operating_system.version_label}`
}

export function readTargetPlatformProfile(): TargetPlatformProfile {
  if (typeof window === 'undefined') return createTargetPlatformProfile()
  try {
    return normalizeTargetPlatformProfile(JSON.parse(window.localStorage.getItem(targetPlatformProfileStorageKey) ?? '{}'))
  } catch {
    return createTargetPlatformProfile()
  }
}

export function writeTargetPlatformProfile(profile: TargetPlatformProfile): void {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(targetPlatformProfileStorageKey, JSON.stringify(profile))
  } catch {
    // This preference is convenient, not required for a valid context pack.
  }
}
