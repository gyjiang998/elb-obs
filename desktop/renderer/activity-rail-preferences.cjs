const ACTIVITY_RAIL_PREFERENCE_VERSION = 1;
const ACTIVITY_RAIL_STORAGE_KEY = 'hetero.desktop.activity-rail.v1';
const DEFAULT_ACTIVITY_RAIL_PREFERENCES = Object.freeze({
  version: ACTIVITY_RAIL_PREFERENCE_VERSION,
  expandOnHover: true,
});

function normalizeActivityRailPreferences(value) {
  if (!value || value.version !== ACTIVITY_RAIL_PREFERENCE_VERSION) return {...DEFAULT_ACTIVITY_RAIL_PREFERENCES};
  return {
    version: ACTIVITY_RAIL_PREFERENCE_VERSION,
    expandOnHover: typeof value.expandOnHover === 'boolean' ? value.expandOnHover : true,
  };
}

function readActivityRailPreferences(storage) {
  try {
    return normalizeActivityRailPreferences(JSON.parse(storage?.getItem?.(ACTIVITY_RAIL_STORAGE_KEY) || 'null'));
  } catch {
    return {...DEFAULT_ACTIVITY_RAIL_PREFERENCES};
  }
}

function writeActivityRailPreferences(storage, value) {
  const normalized = normalizeActivityRailPreferences({
    version: ACTIVITY_RAIL_PREFERENCE_VERSION,
    ...value,
  });
  if (!storage?.setItem) throw new Error('Activity rail preference storage is unavailable');
  storage.setItem(ACTIVITY_RAIL_STORAGE_KEY, JSON.stringify(normalized));
  return normalized;
}

module.exports = {
  ACTIVITY_RAIL_PREFERENCE_VERSION,
  ACTIVITY_RAIL_STORAGE_KEY,
  DEFAULT_ACTIVITY_RAIL_PREFERENCES,
  normalizeActivityRailPreferences,
  readActivityRailPreferences,
  writeActivityRailPreferences,
};
