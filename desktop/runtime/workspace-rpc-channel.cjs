'use strict';

const crypto = require('node:crypto');
const {EventEmitter} = require('node:events');

const SCHEMA_VERSION = 'remote_workspace_rpc/v1';
const STREAM_PROTOCOL = 'remote_workspace_rpc_stream/v1';
const READY_LINE = `HETERO_WORKSPACE_RPC_READY\t${STREAM_PROTOCOL}`;
const REQUEST_HEADER = 'HETERO_WORKSPACE_RPC_REQUEST\t';
const RESPONSE_HEADER = 'HETERO_WORKSPACE_RPC_RESPONSE\t';
const MAX_HEADER_BYTES = 64 * 1024;
// A legal 1 MiB UTF-8 text document can approach 2 MiB after JSON escaping
// (for example, a file made entirely of backslashes). Keep enough framing
// headroom for metadata and the request envelope.
const MAX_REQUEST_BYTES = 4 * 1024 * 1024;
const MAX_RESPONSE_BYTES = 8 * 1024 * 1024;
const MAX_DIAGNOSTIC_BYTES = 1024 * 1024;
const MAX_PENDING_REQUESTS = 64;
const MAX_REQUEST_TIMEOUT_MS = 31 * 60 * 1000;
const MAX_INFLIGHT_REQUEST_BYTES = 16 * 1024 * 1024;

function boundedAppend(current, value, limit = MAX_DIAGNOSTIC_BYTES) {
  const incoming = Buffer.isBuffer(value) ? value.toString('utf8') : String(value || '');
  const next = current + incoming;
  if (Buffer.byteLength(next) <= limit) return next;
  return Buffer.from(next).subarray(-limit).toString('utf8');
}

function protocolError(code, message, details) {
  const error = new Error(message);
  error.name = 'WorkspaceRpcError';
  error.code = code;
  if (details !== undefined) error.details = details;
  return error;
}

function parseFrameLength(value, maximum) {
  if (!/^(?:0|[1-9][0-9]{0,9})$/.test(value))
    throw protocolError('WORKSPACE_RPC_INVALID_FRAME', '远端文件服务返回了无效的帧长度。');
  const length = Number(value);
  if (!Number.isSafeInteger(length) || length > maximum)
    throw protocolError('WORKSPACE_RPC_FRAME_TOO_LARGE', `远端文件服务响应超过 ${maximum} 字节限制。`);
  return length;
}

class WorkspaceRpcChannel extends EventEmitter {
  constructor({client, command, generation = 0, key = 'workspace', readyTimeoutMs = 30000,
    requestTimeoutMs = 30000, maxPendingRequests = MAX_PENDING_REQUESTS, bootstrapPayload = null} = {}) {
    super();
    if (!client || typeof client.exec !== 'function') throw new TypeError('WorkspaceRpcChannel requires an SSH client.');
    if (typeof command !== 'string' || !command.trim()) throw new TypeError('WorkspaceRpcChannel requires a startup command.');
    this.client = client;
    this.command = command;
    this.generation = generation;
    this.key = key;
    this.readyTimeoutMs = readyTimeoutMs;
    this.requestTimeoutMs = requestTimeoutMs;
    this.maxPendingRequests = maxPendingRequests;
    if (bootstrapPayload !== null && !Buffer.isBuffer(bootstrapPayload))
      throw new TypeError('WorkspaceRpcChannel bootstrapPayload must be a Buffer.');
    if (bootstrapPayload && bootstrapPayload.length > MAX_REQUEST_BYTES)
      throw new RangeError('WorkspaceRpcChannel bootstrapPayload is too large.');
    this.bootstrapPayload = bootstrapPayload;
    this.state = 'opening';
    this.stream = null;
    this.buffer = Buffer.alloc(0);
    this.expectedBodyBytes = null;
    this.stderr = '';
    this.stdoutNoise = '';
    this.pending = new Map();
    // Callers may deliberately reuse a logical request ID after cancellation.
    // Every wire attempt gets a fresh ID so a delayed response from the old
    // attempt can never resolve the new promise.
    this.logicalPending = new Map();
    this.inflightRequestBytes = 0;
    this.startedAt = Date.now();
    this.requestCount = 0;
    this.ready = this.open();
    // A caller may create the channel and await it later. Keep Node from
    // treating an early SSH failure as an unhandled rejection.
    this.ready.catch(() => {});
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
        const error = protocolError('WORKSPACE_RPC_READY_TIMEOUT', '远端文件服务初始化超时。');
        finishReady(error);
        this.close(error);
      }, this.readyTimeoutMs);
      try {
        this.client.exec(this.command, (error, stream) => {
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
          stream.on('data', data => this.onStdout(data));
          stream.stderr?.on('data', data => { this.stderr = boundedAppend(this.stderr, data); });
          stream.on('error', channelError => this.close(channelError));
          stream.on('close', (code, signal) => {
            const message = this.stderr.trim()
              || `远端文件服务已退出（exit ${Number.isInteger(code) ? code : 'unknown'}）。`;
            const channelError = protocolError('WORKSPACE_RPC_CHANNEL_CLOSED', message, {
              exitCode: Number.isInteger(code) ? code : null, signal: signal || null,
            });
            finishReady(channelError);
            this.close(channelError, {closeStream: false});
          });
          if (this.bootstrapPayload?.length) {
            try { stream.write(this.bootstrapPayload); }
            catch (bootstrapError) {
              finishReady(bootstrapError);
              this.close(bootstrapError);
            }
          }
        });
      } catch (error) {
        finishReady(error);
        this.close(error, {closeStream: false});
      }
    });
  }

  onStdout(data) {
    if (this.state === 'closed') return;
    const bytes = Buffer.isBuffer(data) ? data : Buffer.from(String(data || ''), 'utf8');
    this.buffer = this.buffer.length ? Buffer.concat([this.buffer, bytes]) : Buffer.from(bytes);
    try { this.consumeFrames(); }
    catch (error) { this.close(error); }
  }

  consumeFrames() {
    while (this.buffer.length) {
      if (this.expectedBodyBytes !== null) {
        if (this.buffer.length < this.expectedBodyBytes) return;
        const body = this.buffer.subarray(0, this.expectedBodyBytes);
        this.buffer = this.buffer.subarray(this.expectedBodyBytes);
        this.expectedBodyBytes = null;
        this.consumeResponse(body);
        continue;
      }
      const newline = this.buffer.indexOf(0x0a);
      if (newline < 0) {
        if (this.buffer.length > MAX_HEADER_BYTES)
          throw protocolError('WORKSPACE_RPC_INVALID_FRAME', '远端文件服务输出了过大的未分帧数据。');
        return;
      }
      const rawLine = this.buffer.subarray(0, newline);
      this.buffer = this.buffer.subarray(newline + 1);
      const line = rawLine.toString('utf8').replace(/\r$/, '');
      if (line === READY_LINE) {
        if (this.state === 'opening') {
          this.state = 'ready';
          this.finishReady?.();
          this.emit('ready');
        }
        continue;
      }
      if (line.startsWith(RESPONSE_HEADER)) {
        this.expectedBodyBytes = parseFrameLength(line.slice(RESPONSE_HEADER.length), MAX_RESPONSE_BYTES);
        continue;
      }
      // A shell profile or accidental print must never be interpreted as a
      // protocol response. Retain it only as bounded diagnostics.
      this.stdoutNoise = boundedAppend(this.stdoutNoise, `${line}\n`);
    }
  }

  consumeResponse(body) {
    let response;
    try { response = JSON.parse(body.toString('utf8')); }
    catch (error) {
      throw protocolError('WORKSPACE_RPC_INVALID_RESPONSE', '远端文件服务返回了无效的 JSON 响应。', error.message);
    }
    if (!response || response.schema_version !== SCHEMA_VERSION || typeof response.id !== 'string'
      || typeof response.ok !== 'boolean')
      throw protocolError('WORKSPACE_RPC_INVALID_RESPONSE', '远端文件服务响应不符合协议。');
    const pending = this.pending.get(response.id);
    if (!pending) return;
    this.pending.delete(response.id);
    if (this.logicalPending.get(pending.logicalRequestId) === response.id)
      this.logicalPending.delete(pending.logicalRequestId);
    this.inflightRequestBytes -= pending.requestBytes;
    clearTimeout(pending.timer);
    if (response.ok) {
      pending.resolve({
        result: response.result, requestId: pending.logicalRequestId,
        durationMs: Date.now() - pending.startedAt, channelReused: pending.channelReused,
      });
      return;
    }
    const remote = response.error || {};
    pending.reject(protocolError(
      typeof remote.code === 'string' ? remote.code : 'WORKSPACE_RPC_REMOTE_ERROR',
      typeof remote.message === 'string' ? remote.message : '远端文件服务执行失败。',
      remote.details,
    ));
  }

  async request({requestId = crypto.randomUUID(), method, params = {}, timeoutMs = this.requestTimeoutMs,
    internal = false} = {}) {
    await this.ready;
    if (this.state !== 'ready' || !this.stream) throw protocolError('WORKSPACE_RPC_UNAVAILABLE', '远端文件服务不可用。');
    if (typeof requestId !== 'string' || !/^[A-Za-z0-9._:-]{1,128}$/.test(requestId))
      throw protocolError('WORKSPACE_RPC_INVALID_REQUEST', '远端文件请求 ID 无效。');
    if (typeof method !== 'string' || !/^[A-Za-z][A-Za-z0-9.]{0,63}$/.test(method))
      throw protocolError('WORKSPACE_RPC_INVALID_REQUEST', '远端文件方法无效。');
    if (!params || typeof params !== 'object' || Array.isArray(params))
      throw protocolError('WORKSPACE_RPC_INVALID_REQUEST', '远端文件请求参数必须是对象。');
    if (this.logicalPending.has(requestId)) throw protocolError('WORKSPACE_RPC_DUPLICATE_ID', '同一远端文件请求 ID 已在执行。');
    if (this.pending.size >= this.maxPendingRequests)
      throw protocolError('WORKSPACE_RPC_BUSY', '远端文件服务并发请求过多。');
    const wireRequestId = `wire-${crypto.randomUUID()}`;
    const body = Buffer.from(JSON.stringify({schema_version: SCHEMA_VERSION, id: wireRequestId, method, params}), 'utf8');
    if (body.length > MAX_REQUEST_BYTES)
      throw protocolError('WORKSPACE_RPC_REQUEST_TOO_LARGE', `远端文件请求超过 ${MAX_REQUEST_BYTES} 字节限制。`);
    if (this.inflightRequestBytes + body.length > MAX_INFLIGHT_REQUEST_BYTES)
      throw protocolError('WORKSPACE_RPC_BUSY', '远端 Workspace 通道的在途请求数据超过限制。');
    if (!Number.isFinite(timeoutMs) || timeoutMs < 100 || timeoutMs > MAX_REQUEST_TIMEOUT_MS)
      throw protocolError('WORKSPACE_RPC_INVALID_REQUEST', `远端 Workspace 请求超时必须在 100 至 ${MAX_REQUEST_TIMEOUT_MS} ms 之间。`);
    const channelReused = this.requestCount > 0;
    if (!internal) this.requestCount += 1;
    return new Promise((resolve, reject) => {
      const startedAt = Date.now();
      const timer = setTimeout(() => {
        const pending = this.pending.get(wireRequestId);
        if (!pending) return;
        this.pending.delete(wireRequestId);
        if (this.logicalPending.get(requestId) === wireRequestId) this.logicalPending.delete(requestId);
        this.inflightRequestBytes -= pending.requestBytes;
        const error = protocolError('WORKSPACE_RPC_REQUEST_TIMEOUT', `远端 Workspace 请求超过 ${timeoutMs} ms，已取消该请求。`);
        reject(error);
        this.sendCancellation(wireRequestId);
      }, timeoutMs);
      this.pending.set(wireRequestId, {resolve, reject, timer, startedAt, channelReused,
        requestBytes: body.length, logicalRequestId: requestId});
      this.logicalPending.set(requestId, wireRequestId);
      this.inflightRequestBytes += body.length;
      const header = Buffer.from(`${REQUEST_HEADER}${body.length}\n`, 'ascii');
      try { this.stream.write(Buffer.concat([header, body])); }
      catch (error) { this.close(error); }
    });
  }

  sendCancellation(targetRequestId) {
    if (this.state !== 'ready' || !this.stream) return false;
    const request = {
      schema_version: SCHEMA_VERSION,
      id: `cancel-${crypto.randomUUID()}`,
      method: 'rpc.cancel', params: {requestId: targetRequestId},
    };
    const body = Buffer.from(JSON.stringify(request), 'utf8');
    const header = Buffer.from(`${REQUEST_HEADER}${body.length}\n`, 'ascii');
    try { this.stream.write(Buffer.concat([header, body])); return true; }
    catch { return false; }
  }

  cancel(requestId, reason = protocolError('WORKSPACE_RPC_CANCELLED', '远端 Workspace 请求已取消。')) {
    const wireRequestId = this.logicalPending.get(requestId);
    if (!wireRequestId) return false;
    const pending = this.pending.get(wireRequestId);
    if (!pending) return false;
    this.pending.delete(wireRequestId);
    this.logicalPending.delete(requestId);
    this.inflightRequestBytes -= pending.requestBytes;
    clearTimeout(pending.timer);
    pending.reject(reason);
    this.sendCancellation(wireRequestId);
    return true;
  }

  close(reason = protocolError('WORKSPACE_RPC_CHANNEL_CLOSED', '远端文件服务已关闭。'), {closeStream = true} = {}) {
    if (this.state === 'closed') return;
    this.state = 'closed';
    this.finishReady?.(reason);
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(reason);
    }
    this.pending.clear();
    this.logicalPending.clear();
    this.inflightRequestBytes = 0;
    if (closeStream && this.stream) {
      try { this.stream.signal?.('TERM'); } catch {}
      try { this.stream.end?.(); } catch {}
      try { this.stream.close?.(); } catch {}
    }
    this.emit('closed', reason);
  }
}

module.exports = {
  MAX_REQUEST_BYTES,
  MAX_RESPONSE_BYTES,
  MAX_INFLIGHT_REQUEST_BYTES,
  READY_LINE,
  REQUEST_HEADER,
  RESPONSE_HEADER,
  SCHEMA_VERSION,
  STREAM_PROTOCOL,
  WorkspaceRpcChannel,
};
