'use strict';

const {EventEmitter} = require('node:events');

const READY_MARKER = 'HETERO_DESKTOP_RPC_STREAM_READY\t';
const RESPONSE_MARKER = 'HETERO_DESKTOP_RPC_STREAM_RESPONSE\t';
const STREAM_PROTOCOL = 'desktop_platform_rpc_stream/v1';
const MAX_LINE_BYTES = 2 * 1024 * 1024;
const MAX_DIAGNOSTIC_BYTES = 1024 * 1024;

function boundedAppend(current, value, limit = MAX_DIAGNOSTIC_BYTES) {
  const next = current + String(value || '');
  return Buffer.byteLength(next) <= limit ? next : next.slice(-limit);
}

class PlatformRpcChannel extends EventEmitter {
  constructor({client, command, cwdPrefix, generation, key, readyTimeoutMs = 30000}) {
    super();
    this.client = client;
    this.command = command;
    this.cwdPrefix = cwdPrefix;
    this.generation = generation;
    this.key = key;
    this.readyTimeoutMs = readyTimeoutMs;
    this.state = 'opening';
    this.stream = null;
    this.stdoutBuffer = '';
    this.stderr = '';
    this.pending = new Map();
    this.startedAt = Date.now();
    this.reusedRequests = 0;
    this.ready = this.open();
  }

  open() {
    return new Promise((resolve, reject) => {
      let settled = false;
      const finishReady = error => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (error) reject(error); else resolve(this);
      };
      this.finishReady = finishReady;
      const timer = setTimeout(() => {
        const error = new Error('服务器分析长驻通道初始化超时。');
        error.code = 'PLATFORM_RPC_CHANNEL_TIMEOUT';
        finishReady(error);
        this.close(error);
      }, this.readyTimeoutMs);
      try {
        this.client.exec(this.cwdPrefix + this.command, (error, stream) => {
          if (error) {
            finishReady(error);
            this.close(error, {closeStream: false});
            return;
          }
          if (this.state === 'closed') {
            try { stream.close(); } catch {}
            return;
          }
          this.stream = stream;
          stream.setEncoding?.('utf8');
          stream.stderr?.setEncoding?.('utf8');
          stream.on('data', data => this.onStdout(data));
          stream.stderr?.on('data', data => { this.stderr = boundedAppend(this.stderr, data); });
          stream.on('error', channelError => this.close(channelError));
          stream.on('close', (code, signal) => {
            const error = new Error(this.stderr.trim() || `服务器分析长驻通道已退出（exit ${Number.isInteger(code) ? code : 'unknown'}）。`);
            error.code = 'PLATFORM_RPC_CHANNEL_CLOSED';
            error.remoteResult = {
              stdout: '', stderr: this.stderr, exitCode: Number.isInteger(code) ? code : 1,
              signal: signal || null, durationMs: Date.now() - this.startedAt,
            };
            finishReady(error);
            this.close(error, {closeStream: false});
          });
        });
      } catch (error) {
        finishReady(error);
        this.close(error, {closeStream: false});
      }
    });
  }

  onStdout(data) {
    this.stdoutBuffer += String(data || '');
    if (Buffer.byteLength(this.stdoutBuffer) > MAX_LINE_BYTES) {
      const error = new Error('服务器分析长驻通道返回了过大的未分帧数据。');
      error.code = 'PLATFORM_RPC_FRAME_TOO_LARGE';
      this.close(error);
      return;
    }
    let newline;
    while ((newline = this.stdoutBuffer.indexOf('\n')) >= 0) {
      const line = this.stdoutBuffer.slice(0, newline).replace(/\r$/, '');
      this.stdoutBuffer = this.stdoutBuffer.slice(newline + 1);
      if (line === `${READY_MARKER}${STREAM_PROTOCOL}`) {
        if (this.state === 'opening') {
          this.state = 'ready';
          this.finishReady?.();
          this.emit('ready');
        }
        continue;
      }
      if (!line.startsWith(RESPONSE_MARKER)) continue;
      const frame = line.slice(RESPONSE_MARKER.length);
      const separator = frame.indexOf('\t');
      if (separator < 1) continue;
      const requestId = frame.slice(0, separator);
      const pending = this.pending.get(requestId);
      if (!pending) continue;
      this.pending.delete(requestId);
      clearTimeout(pending.timer);
      pending.resolve({
        stdout: `${frame.slice(separator + 1)}\n`, stderr: '', exitCode: 0, signal: null,
        durationMs: Date.now() - pending.startedAt, channelReused: pending.channelReused,
      });
    }
  }

  async request({requestId, requestBase64, timeoutMs}) {
    await this.ready;
    if (this.state !== 'ready' || !this.stream) throw new Error('服务器分析长驻通道不可用。');
    if (this.pending.has(requestId)) throw new Error('同一平台请求 ID 已在执行。');
    const channelReused = this.reusedRequests > 0;
    this.reusedRequests += 1;
    return new Promise((resolve, reject) => {
      const startedAt = Date.now();
      const timer = setTimeout(() => {
        const error = new Error(`服务器分析请求超过 ${timeoutMs} ms，已重置长驻通道。`);
        error.code = 'PLATFORM_RPC_REQUEST_TIMEOUT';
        this.close(error);
      }, timeoutMs);
      this.pending.set(requestId, {resolve, reject, timer, startedAt, channelReused});
      try {
        this.stream.write(`${JSON.stringify({request_id: requestId, request_base64: requestBase64})}\n`);
      } catch (error) {
        this.close(error);
      }
    });
  }

  cancel(requestId, reason = new Error('服务器分析请求已取消。')) {
    if (!this.pending.has(requestId)) return false;
    // The Python dispatcher is synchronous. Closing the worker is the only
    // reliable way to stop a running analysis without letting stale writes
    // mutate its state after the UI has moved on.
    this.close(reason);
    return true;
  }

  close(reason = new Error('服务器分析长驻通道已关闭。'), {closeStream = true} = {}) {
    if (this.state === 'closed') return;
    this.state = 'closed';
    if (this.finishReady) this.finishReady(reason);
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(reason);
    }
    this.pending.clear();
    if (closeStream && this.stream) {
      try { this.stream.signal?.('TERM'); } catch {}
      try { this.stream.close(); } catch {}
    }
    this.emit('closed', reason);
  }
}

module.exports = {
  MAX_LINE_BYTES,
  PlatformRpcChannel,
  READY_MARKER,
  RESPONSE_MARKER,
  STREAM_PROTOCOL,
};
