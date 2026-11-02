#!/usr/bin/env node
'use strict';
const readline = require('node:readline');
const {callDesktop} = require('./agent-tools.cjs');
process.stdout.write('\r\n连接测试客户端（不是 AI Agent）\r\n输入任意说明并回车，将通过桌面工具执行 pwd、uname -a 和 ls -la。\r\n> ');
let busy = false;
const lines = readline.createInterface({input: process.stdin, terminal: false});
lines.on('line', async line => {
  if (busy || !line.trim()) return;
  busy = true;
  try {
    process.stdout.write('\r\n[工具调用] remote_status\r\n');
    const state = await callDesktop('remote_status');
    process.stdout.write(`${JSON.stringify(state)}\r\n`);
    process.stdout.write('[工具调用] remote_exec: pwd && uname -a && ls -la\r\n');
    const result = await callDesktop('remote_exec', {command: 'pwd && uname -a && ls -la', timeoutMs: 15000});
    process.stdout.write(`${result.stdout || ''}${result.stderr || ''}\r\n[退出码 ${result.exitCode}，${result.durationMs} ms]\r\n`);
  } catch (error) { process.stdout.write(`[失败] ${error.message}\r\n`); }
  finally { busy = false; process.stdout.write('> '); }
});
