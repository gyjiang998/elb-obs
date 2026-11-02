'use strict';

function tabIsDirty(tab) {
  return Boolean(tab) && String(tab.content ?? '') !== String(tab.pristine ?? '');
}

function tabNeedsRead(opened) {
  return Boolean(opened?.created || opened?.tab?.status === 'error'
    || (opened?.tab?.status === 'loading' && !opened?.tab?.busy));
}

class RemoteEditorTabStore {
  constructor() {
    this.items = [];
    this.activeId = '';
    this.nextId = 1;
  }

  get active() { return this.items.find(tab => tab.id === this.activeId) || null; }
  get all() { return this.items; }
  get(id) { return this.items.find(tab => tab.id === id) || null; }
  find(identity, path) {
    return this.items.find(tab => !tab.detached && tab.identity === identity && tab.path === path) || null;
  }
  activate(id) {
    if (!this.get(id)) return null;
    this.activeId = id;
    return this.active;
  }
  open({identity, path, entry = {}, rootPath = '~'} = {}) {
    const existing = this.find(identity, path);
    if (existing) {
      this.activeId = existing.id;
      return {tab: existing, created: false};
    }
    const tab = {
      id: `remote-tab-${this.nextId++}`,
      identity,
      path,
      rootPath,
      name: entry.name || String(path || '').split('/').at(-1) || String(path || ''),
      entry: {...entry},
      content: '',
      pristine: '',
      revision: '',
      revisionNeedsVerification: false,
      detached: false,
      busy: 'read',
      status: 'loading',
      error: '',
      selectionStart: 0,
      selectionEnd: 0,
      scrollTop: 0,
    };
    this.items.push(tab);
    this.activeId = tab.id;
    return {tab, created: true};
  }
  close(id) {
    const index = this.items.findIndex(tab => tab.id === id);
    if (index === -1) return null;
    const [removed] = this.items.splice(index, 1);
    if (this.activeId === id) {
      this.activeId = this.items[index]?.id || this.items[index - 1]?.id || '';
    }
    return removed;
  }
  dirtyTabs() { return this.items.filter(tabIsDirty); }
  hasDirty() { return this.items.some(tabIsDirty); }

  transitionToIdentity(nextIdentity) {
    const retained = [];
    const removed = [];
    const reactivated = [];
    for (const tab of [...this.items]) {
      if (tab.identity === nextIdentity) {
        if (tab.detached) {
          tab.detached = false;
          tab.revisionNeedsVerification = tabIsDirty(tab);
          reactivated.push(tab);
        }
        continue;
      }
      if (tab.detached) continue;
      if (tabIsDirty(tab)) {
        tab.detached = true;
        tab.busy = '';
        tab.revisionNeedsVerification = false;
        retained.push(tab);
      } else {
        removed.push(tab);
        this.close(tab.id);
      }
    }
    const current = this.active;
    if (!current || current.detached) {
      const candidate = reactivated.at(-1)
        || this.items.find(tab => !tab.detached && tab.identity === nextIdentity);
      if (candidate) this.activeId = candidate.id;
    }
    return {retained, removed, reactivated};
  }

  transitionSourceIdentity(nextIdentity, source) {
    const retained = [];
    const removed = [];
    const reactivated = [];
    for (const tab of [...this.items]) {
      if (tab.source !== source) continue;
      if (tab.identity === nextIdentity) {
        if (tab.detached) {
          tab.detached = false;
          tab.revisionNeedsVerification = tabIsDirty(tab);
          reactivated.push(tab);
        }
        continue;
      }
      if (tab.detached) continue;
      if (tabIsDirty(tab)) {
        tab.detached = true;
        tab.busy = '';
        tab.revisionNeedsVerification = false;
        retained.push(tab);
      } else {
        removed.push(tab);
        this.close(tab.id);
      }
    }
    const current = this.active;
    if (!current || (current.source === source && current.detached)) {
      const candidate = reactivated.at(-1)
        || this.items.find(tab => !tab.detached && tab.identity === nextIdentity);
      if (candidate) this.activeId = candidate.id;
    }
    return {retained, removed, reactivated};
  }
}

module.exports = {RemoteEditorTabStore, tabIsDirty, tabNeedsRead};
