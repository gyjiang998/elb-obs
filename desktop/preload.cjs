const { contextBridge, ipcRenderer, webUtils } = require('electron');
contextBridge.exposeInMainWorld('desktop', {
  invoke: (method, payload) => ipcRenderer.invoke('desktop:invoke', method, payload),
  // Keep native paths inside the isolated preload/Main boundary. The renderer
  // receives a FileList from the OS drop but never gets the workstation path,
  // and the generic desktop:invoke bridge cannot manufacture an upload.
  uploadDroppedFiles: (files, payload = {}) => {
    const values = Array.from(files || []);
    if (!values.length || values.length > 64) throw new Error('请选择 1–64 个本地文件上传。');
    const localPaths = values.map(file => webUtils.getPathForFile(file));
    if (localPaths.some(value => !value)) throw new Error('拖入内容不是可读取的本地文件。');
    return ipcRenderer.invoke('desktop:upload-dropped-files', {
      localPaths,
      directory: String(payload?.directory || ''),
      expectedConnection: payload?.expectedConnection,
    });
  },
  onEvent: (callback) => {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on('desktop:event', listener);
    return () => ipcRenderer.removeListener('desktop:event', listener);
  },
  onMenuAction: (callback) => {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on('desktop:menu-action', listener);
    return () => ipcRenderer.removeListener('desktop:menu-action', listener);
  },
});
