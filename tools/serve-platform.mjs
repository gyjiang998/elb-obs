#!/usr/bin/env node

import { EventEmitter } from 'node:events'
import { createServer } from 'node:http'
import { connect as connectTcp } from 'node:net'
import next from 'next'

function option(name, fallback) {
  const index = process.argv.indexOf(name)
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback
}

const dev = process.argv.includes('--dev')
const hostname = option('--hostname', option('-H', '127.0.0.1'))
const port = Number.parseInt(option('--port', option('-p', '4032')), 10)
const apiTarget = new URL(process.env.HETERO_NEXT_API_PROXY_TARGET ?? 'http://127.0.0.1:8006')

if (!Number.isInteger(port) || port < 1 || port > 65535) {
  throw new Error(`Invalid Web port: ${port}`)
}
if (apiTarget.protocol !== 'http:') {
  throw new Error(`HETERO_NEXT_API_PROXY_TARGET must use http for the loopback API: ${apiTarget.origin}`)
}

// Next's custom-server wrapper lazily attaches its own Upgrade listener to the
// HTTP server observed on the first request.  Register that listener on a sink
// and route upgrades explicitly below; otherwise the Device Bridge request is
// proxied twice and the second HTTP 101 is parsed as a WebSocket data frame.
const nextUpgradeRegistrationSink = new EventEmitter()
const app = next({ dev, hostname, port, httpServer: nextUpgradeRegistrationSink })
const handle = app.getRequestHandler()

function rejectUpgrade(socket, status, message) {
  if (!socket.destroyed) {
    socket.end(`HTTP/1.1 ${status}\r\nConnection: close\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: ${Buffer.byteLength(message)}\r\n\r\n${message}`)
  }
}

function proxyDeviceBridgeUpgrade(request, clientSocket, head) {
  let pathname = ''
  try {
    pathname = new URL(request.url ?? '/', 'http://platform.invalid').pathname
  } catch {
    rejectUpgrade(clientSocket, '400 Bad Request', 'Invalid WebSocket request path')
    return
  }
  if (pathname !== '/v1/device-bridges/ws') {
    rejectUpgrade(clientSocket, '404 Not Found', 'WebSocket endpoint not found')
    return
  }

  const upstreamPort = Number.parseInt(apiTarget.port || '80', 10)
  const upstream = connectTcp({ host: apiTarget.hostname, port: upstreamPort })
  const closeBoth = () => {
    if (!clientSocket.destroyed) clientSocket.destroy()
    if (!upstream.destroyed) upstream.destroy()
  }

  upstream.once('connect', () => {
    const requestPath = `${apiTarget.pathname.replace(/\/$/, '')}${request.url ?? '/'}` || '/'
    const headers = []
    for (let index = 0; index < request.rawHeaders.length; index += 2) {
      const name = request.rawHeaders[index]
      const value = request.rawHeaders[index + 1] ?? ''
      headers.push(`${name}: ${name.toLowerCase() === 'host' ? apiTarget.host : value}`)
    }
    upstream.write(`${request.method ?? 'GET'} ${requestPath} HTTP/${request.httpVersion}\r\n${headers.join('\r\n')}\r\n\r\n`)
    if (head.length > 0) upstream.write(head)
    clientSocket.pipe(upstream)
    upstream.pipe(clientSocket)
  })
  upstream.once('error', () => {
    if (!clientSocket.destroyed) rejectUpgrade(clientSocket, '502 Bad Gateway', 'Platform API WebSocket unavailable')
    closeBoth()
  })
  clientSocket.once('error', closeBoth)
  clientSocket.once('close', () => {
    if (!upstream.destroyed) upstream.destroy()
  })
  upstream.once('close', () => {
    if (!clientSocket.destroyed) clientSocket.destroy()
  })
}

await app.prepare()
const nextUpgradeHandler = app.getUpgradeHandler()

const server = createServer((request, response) => {
  void handle(request, response)
})
server.on('upgrade', (request, socket, head) => {
  let pathname = ''
  try {
    pathname = new URL(request.url ?? '/', 'http://platform.invalid').pathname
  } catch {
    rejectUpgrade(socket, '400 Bad Request', 'Invalid WebSocket request path')
    return
  }
  if (pathname === '/v1/device-bridges/ws') {
    proxyDeviceBridgeUpgrade(request, socket, head)
    return
  }
  void nextUpgradeHandler(request, socket, head).catch(() => {
    if (!socket.destroyed) rejectUpgrade(socket, '502 Bad Gateway', 'Next.js WebSocket handler unavailable')
  })
})
server.on('clientError', (_error, socket) => rejectUpgrade(socket, '400 Bad Request', 'Invalid HTTP request'))

server.listen(port, hostname, () => {
  const displayHost = hostname === '0.0.0.0' ? 'localhost' : hostname
  console.log(`Platform Web ready: http://${displayHost}:${port}`)
  console.log(`Device Bridge HTTP poll uses /v1/device-bridges/poll; WebSocket compatibility proxy: /v1/device-bridges/ws -> ${apiTarget.origin}`)
})

function shutdown() {
  server.close(() => process.exit(0))
  setTimeout(() => process.exit(0), 5000).unref()
}

process.once('SIGINT', shutdown)
process.once('SIGTERM', shutdown)
