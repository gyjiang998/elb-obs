const test = require('node:test');
const assert = require('node:assert/strict');
const {
  ACTIVITY_RAIL_STORAGE_KEY,
  normalizeActivityRailPreferences,
  readActivityRailPreferences,
  writeActivityRailPreferences,
} = require('../renderer/activity-rail-preferences.cjs');

function storage(initial = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem: key => values.has(key) ? values.get(key) : null,
    setItem: (key, value) => values.set(key, String(value)),
    value: key => values.get(key),
  };
}

test('activity rail expansion defaults on and rejects stale or malformed preferences', () => {
  assert.deepEqual(normalizeActivityRailPreferences(null), {version: 1, expandOnHover: true});
  assert.deepEqual(normalizeActivityRailPreferences({version: 0, expandOnHover: false}), {version: 1, expandOnHover: true});
  assert.deepEqual(normalizeActivityRailPreferences({version: 1, expandOnHover: 'false'}), {version: 1, expandOnHover: true});
  const broken = storage({[ACTIVITY_RAIL_STORAGE_KEY]: '{broken'});
  assert.deepEqual(readActivityRailPreferences(broken), {version: 1, expandOnHover: true});
});

test('activity rail expansion can be disabled and survives renderer reload', () => {
  const local = storage();
  assert.deepEqual(writeActivityRailPreferences(local, {expandOnHover: false}), {version: 1, expandOnHover: false});
  assert.deepEqual(JSON.parse(local.value(ACTIVITY_RAIL_STORAGE_KEY)), {version: 1, expandOnHover: false});
  assert.deepEqual(readActivityRailPreferences(local), {version: 1, expandOnHover: false});
});

test('activity rail save reports browser storage failures instead of claiming persistence', () => {
  const unavailable = {setItem() { throw new Error('quota denied'); }};
  assert.throws(() => writeActivityRailPreferences(unavailable, {expandOnHover: false}), /quota denied/);
});
