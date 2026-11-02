import * as monaco from 'monaco-editor/esm/vs/editor/edcore.main.js';
import 'monaco-editor/esm/vs/basic-languages/monaco.contribution.js';
import editorContract from './remote-code-editor-contract.cjs';

const { languageFromPath, modelUriFor, monacoThemeFor } = editorContract;
const DEFAULT_VIEW = Object.freeze({ selectionStart: 0, selectionEnd: 0, scrollTop: 0 });

function configureMonacoWorker() {
  const existing = globalThis.MonacoEnvironment || {};
  if (typeof existing.getWorker === 'function' || typeof existing.getWorkerUrl === 'function') return;
  globalThis.MonacoEnvironment = {
    ...existing,
    getWorker(_moduleId, label) {
      const url = new URL('./monaco-editor-worker.js', globalThis.location.href);
      return new Worker(url, { name: `monaco-${label || 'editor'}` });
    },
  };
}

function clampOffset(model, value) {
  const offset = Number.isFinite(Number(value)) ? Number(value) : 0;
  return Math.max(0, Math.min(model.getValueLength(), Math.trunc(offset)));
}

function selectionFromOffsets(model, start, end = start) {
  const startOffset = clampOffset(model, start);
  const endOffset = clampOffset(model, end);
  const startPosition = model.getPositionAt(Math.min(startOffset, endOffset));
  const endPosition = model.getPositionAt(Math.max(startOffset, endOffset));
  return new monaco.Selection(
    startPosition.lineNumber,
    startPosition.column,
    endPosition.lineNumber,
    endPosition.column,
  );
}

function viewFromEditor(editor, model) {
  if (!model || editor.getModel() !== model) return null;
  const selection = editor.getSelection();
  const position = editor.getPosition();
  const selectionStart = selection ? model.getOffsetAt(selection.getStartPosition()) : 0;
  const selectionEnd = selection ? model.getOffsetAt(selection.getEndPosition()) : selectionStart;
  return {
    selectionStart,
    selectionEnd,
    scrollTop: editor.getScrollTop(),
    line: position?.lineNumber || 1,
    column: position?.column || 1,
  };
}

/**
 * Monaco-backed editor used by the remote SSH workspace.
 *
 * Each tab owns one Monaco model, so its undo history, selection, folds and
 * scroll position survive tab switches. Server refreshes use forceValue and
 * are suppressed from onChange; ordinary setValue calls behave like edits.
 */
export function createRemoteCodeEditor({
  host,
  onChange = () => {},
  onCursorChange = () => {},
  onSave = () => {},
} = {}) {
  if (!(host instanceof HTMLElement)) throw new TypeError('A Monaco editor host element is required');
  configureMonacoWorker();

  const records = new Map();
  let activeId = '';
  let disposed = false;
  let currentTheme = monacoThemeFor(document.documentElement.dataset.theme);

  const editor = monaco.editor.create(host, {
    model: null,
    theme: currentTheme,
    ariaLabel: '工作区文件编辑器',
    automaticLayout: true,
    bracketPairColorization: { enabled: true },
    cursorBlinking: 'smooth',
    cursorSmoothCaretAnimation: 'on',
    detectIndentation: true,
    dragAndDrop: true,
    fixedOverflowWidgets: true,
    fontFamily: "'Cascadia Code', 'SFMono-Regular', Consolas, 'Liberation Mono', monospace",
    fontLigatures: true,
    fontSize: 13,
    formatOnPaste: false,
    guides: { bracketPairs: true, indentation: true },
    insertSpaces: true,
    minimap: { enabled: true, maxColumn: 100, renderCharacters: false, showSlider: 'mouseover' },
    mouseWheelZoom: true,
    // Monaco 0.53's asynchronous word-highlighter rejects its pending Delayer
    // with a cancellation error when a tab model is disposed. The feature is
    // decorative, while a clean multi-tab lifecycle is part of our contract.
    occurrencesHighlight: 'off',
    padding: { top: 8, bottom: 8 },
    readOnly: true,
    renderWhitespace: 'selection',
    roundedSelection: false,
    scrollBeyondLastLine: false,
    selectionHighlight: false,
    smoothScrolling: true,
    stickyScroll: { enabled: true },
    tabSize: 2,
  });

  const requireRecord = (id = activeId) => {
    const record = records.get(String(id || ''));
    if (!record) throw new Error(`Unknown Monaco model: ${id || '(none)'}`);
    return record;
  };

  const saveActiveViewState = () => {
    const record = records.get(activeId);
    if (!record || editor.getModel() !== record.model) return;
    record.monacoViewState = editor.saveViewState();
    record.view = viewFromEditor(editor, record.model) || record.view;
  };

  const emitCursor = () => {
    const record = records.get(activeId);
    if (!record || editor.getModel() !== record.model) return;
    const view = viewFromEditor(editor, record.model);
    if (!view) return;
    record.view = view;
    onCursorChange({ id: record.id, ...view });
  };

  const activateModel = (id) => {
    if (disposed) return false;
    const record = records.get(String(id || ''));
    if (!record) return false;
    if (activeId !== record.id || editor.getModel() !== record.model) {
      saveActiveViewState();
      activeId = record.id;
      editor.setModel(record.model);
      editor.updateOptions({ readOnly: record.readOnly });
      if (record.monacoViewState) editor.restoreViewState(record.monacoViewState);
      else {
        editor.setSelection(selectionFromOffsets(record.model, record.view.selectionStart, record.view.selectionEnd));
        editor.setScrollTop(record.view.scrollTop || 0);
      }
    } else {
      editor.updateOptions({ readOnly: record.readOnly });
    }
    emitCursor();
    return true;
  };

  const openModel = ({
    id,
    identity = '',
    path = '',
    value = '',
    readOnly = false,
    forceValue = false,
    selectionStart,
    selectionEnd,
    scrollTop,
  } = {}) => {
    if (disposed) throw new Error('The Monaco editor has been disposed');
    const normalizedId = String(id || '');
    if (!normalizedId) throw new TypeError('A model id is required');
    let record = records.get(normalizedId);
    if (!record) {
      const uri = monaco.Uri.parse(modelUriFor({ id: normalizedId, identity, path }));
      const staleModel = monaco.editor.getModel(uri);
      if (staleModel) staleModel.dispose();
      const model = monaco.editor.createModel(String(value ?? ''), languageFromPath(path), uri);
      record = {
        id: normalizedId,
        identity: String(identity || ''),
        path: String(path || ''),
        model,
        readOnly: Boolean(readOnly),
        applyingExternalValue: false,
        monacoViewState: null,
        view: { ...DEFAULT_VIEW },
        disposable: null,
      };
      record.disposable = model.onDidChangeContent(() => {
        if (record.applyingExternalValue || disposed) return;
        onChange({ id: record.id, value: model.getValue() });
      });
      records.set(normalizedId, record);
    } else {
      record.readOnly = Boolean(readOnly);
      if (record.path !== String(path || '')) {
        record.path = String(path || '');
        monaco.editor.setModelLanguage(record.model, languageFromPath(path));
      }
      record.identity = String(identity || record.identity || '');
      if (forceValue && record.model.getValue() !== String(value ?? '')) {
        record.applyingExternalValue = true;
        try { record.model.setValue(String(value ?? '')); }
        finally { record.applyingExternalValue = false; }
        record.monacoViewState = null;
      }
    }
    record.readOnly = Boolean(readOnly);
    if (selectionStart !== undefined) record.view.selectionStart = clampOffset(record.model, selectionStart);
    if (selectionEnd !== undefined) record.view.selectionEnd = clampOffset(record.model, selectionEnd);
    else if (selectionStart !== undefined) record.view.selectionEnd = record.view.selectionStart;
    if (scrollTop !== undefined && Number.isFinite(Number(scrollTop))) record.view.scrollTop = Math.max(0, Number(scrollTop));
    if (selectionStart !== undefined || selectionEnd !== undefined || scrollTop !== undefined) record.monacoViewState = null;
    activateModel(normalizedId);
    return record.model;
  };

  const closeModel = (id) => {
    const normalizedId = String(id || '');
    const record = records.get(normalizedId);
    if (!record) return false;
    if (activeId === normalizedId) {
      saveActiveViewState();
      editor.setModel(null);
      activeId = '';
    }
    record.disposable?.dispose();
    record.model.dispose();
    records.delete(normalizedId);
    return true;
  };

  const getValue = (id = activeId) => requireRecord(id).model.getValue();

  const setValue = (value, id = activeId, { emit = true } = {}) => {
    const record = requireRecord(id);
    const nextValue = String(value ?? '');
    if (record.model.getValue() === nextValue) return false;
    record.applyingExternalValue = !emit;
    try { record.model.setValue(nextValue); }
    finally { record.applyingExternalValue = false; }
    return true;
  };

  const captureViewState = (id = activeId) => {
    const record = requireRecord(id);
    if (record.id === activeId) saveActiveViewState();
    return {
      selectionStart: record.view.selectionStart || 0,
      selectionEnd: record.view.selectionEnd ?? record.view.selectionStart ?? 0,
      scrollTop: record.view.scrollTop || 0,
    };
  };

  const setReadOnly = (value, id = activeId) => {
    const record = requireRecord(id);
    record.readOnly = Boolean(value);
    if (record.id === activeId) editor.updateOptions({ readOnly: record.readOnly });
  };

  const setTheme = (theme) => {
    currentTheme = monacoThemeFor(theme);
    monaco.editor.setTheme(currentTheme);
  };

  const revealPosition = ({line = 1, column = 1} = {}, id = activeId) => {
    const record = requireRecord(id);
    activateModel(record.id);
    const position = {
      lineNumber: Math.max(1, Math.min(record.model.getLineCount(), Math.trunc(Number(line) || 1))),
      column: 1,
    };
    position.column = Math.max(1, Math.min(record.model.getLineMaxColumn(position.lineNumber), Math.trunc(Number(column) || 1)));
    editor.setPosition(position);
    editor.revealPositionInCenter(position);
    editor.focus();
    emitCursor();
  };

  const disposables = [
    editor.onDidChangeCursorSelection(emitCursor),
    editor.onDidScrollChange(event => { if (event.scrollTopChanged) emitCursor(); }),
  ];

  editor.addAction({
    id: 'remote-workspace.save',
    label: '保存工作区文件',
    keybindings: [monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS],
    run: () => {
      const record = records.get(activeId);
      if (record && !record.readOnly) onSave({ id: record.id, value: record.model.getValue() });
    },
  });

  const controller = {
    openModel,
    activateModel,
    closeModel,
    getValue,
    setValue,
    captureViewState,
    setReadOnly,
    getReadOnly: (id = activeId) => requireRecord(id).readOnly,
    setTheme,
    revealPosition,
    focus: () => editor.focus(),
    layout: () => editor.layout(),
    hasModel: id => records.has(String(id || '')),
    getActiveModelId: () => activeId,
    getLanguage: (id = activeId) => requireRecord(id).model.getLanguageId(),
    dispose() {
      if (disposed) return;
      disposed = true;
      for (const disposable of disposables) disposable.dispose();
      editor.setModel(null);
      for (const record of records.values()) {
        record.disposable?.dispose();
        record.model.dispose();
      }
      records.clear();
      editor.dispose();
      if (host.__remoteEditor === testHook) delete host.__remoteEditor;
    },
  };

  const testHook = Object.freeze({
    isMonaco: true,
    getValue,
    setValue: (value, id = activeId) => setValue(value, id, { emit: true }),
    getReadOnly: (id = activeId) => controller.getReadOnly(id),
    getActiveModelId: controller.getActiveModelId,
    getModelCount: () => records.size,
    getLanguage: controller.getLanguage,
    captureViewState,
    focus: controller.focus,
    layout: controller.layout,
  });
  Object.defineProperty(host, '__remoteEditor', { configurable: true, value: testHook });
  return controller;
}

export { languageFromPath, modelUriFor, monacoThemeFor };
