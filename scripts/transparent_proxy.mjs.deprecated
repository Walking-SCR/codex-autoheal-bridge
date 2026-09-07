#!/usr/bin/env node

import { execFileSync } from "node:child_process";
import { extname } from "node:path";
import http from "node:http";
import net from "node:net";
import os from "node:os";

const listenHost = "127.0.0.1";
const listenPort = Number.parseInt(process.env.CODEX_BRIDGE_LISTEN_PORT ?? "8318", 10);
const upstreamHost = "127.0.0.1";
const upstreamPort = Number.parseInt(process.env.CODEX_BRIDGE_UPSTREAM_PORT ?? "8317", 10);
const helper =
  process.env.CODEX_BRIDGE_HELPER ??
  `${process.env.HOME || process.env.USERPROFILE || os.homedir()}/.config/codex-cli-proxy/read-client-key.py`;

function helperCommand(helperPath) {
  if (process.env.CODEX_BRIDGE_HELPER_CMD) {
    let extra = [];
    const raw = process.env.CODEX_BRIDGE_HELPER_ARGS || "";
    if (raw) {
      try {
        const parsed = JSON.parse(raw);
        extra = Array.isArray(parsed) ? parsed.map(String) : [];
      } catch {
        extra = raw.split("\0").filter(Boolean);
      }
    }
    return [process.env.CODEX_BRIDGE_HELPER_CMD, ...extra, helperPath];
  }
  if (extname(helperPath).toLowerCase() === ".py") {
    if (process.platform === "win32") {
      return ["py", "-3", helperPath];
    }
    return [process.env.CODEX_BRIDGE_PYTHON || process.env.PYTHON || "python3", helperPath];
  }
  return [process.env.CODEX_BRIDGE_RUBY || "ruby", helperPath];
}

function readClientKey() {
  const [command, ...args] = helperCommand(helper);
  const value = execFileSync(command, args, {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
    timeout: 5_000,
  }).trim();
  if (!value) throw new Error("CLIProxyAPI client key helper returned no value");
  return value;
}

function upstreamHeaders(headers) {
  return {
    ...headers,
    host: `${upstreamHost}:${upstreamPort}`,
    authorization: `Bearer ${readClientKey()}`,
  };
}

function sendProxyError(response, error) {
  if (response.headersSent) {
    response.destroy(error);
    return;
  }
  response.writeHead(502, { "content-type": "application/json" });
  response.end(JSON.stringify({ error: "Codex transparent proxy is unavailable" }));
}

const server = http.createServer((request, response) => {
  if (request.url === "/__codex_bridge_health") {
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({ status: "ok", upstream: `${upstreamHost}:${upstreamPort}` }));
    return;
  }

  let headers;
  try {
    headers = upstreamHeaders(request.headers);
  } catch (error) {
    sendProxyError(response, error);
    return;
  }

  const upstream = http.request(
    {
      host: upstreamHost,
      port: upstreamPort,
      method: request.method,
      path: request.url,
      headers,
    },
    (upstreamResponse) => {
      response.writeHead(upstreamResponse.statusCode ?? 502, upstreamResponse.headers);
      upstreamResponse.pipe(response);
    },
  );
  upstream.on("error", (error) => sendProxyError(response, error));
  request.on("error", (error) => upstream.destroy(error));
  request.pipe(upstream);
});

server.on("upgrade", (request, socket, head) => {
  let headers;
  try {
    headers = upstreamHeaders(request.headers);
  } catch {
    socket.end("HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n");
    return;
  }

  const upstream = net.connect(upstreamPort, upstreamHost, () => {
    const lines = [`${request.method} ${request.url} HTTP/${request.httpVersion}`];
    for (const [name, value] of Object.entries(headers)) {
      if (Array.isArray(value)) {
        for (const item of value) lines.push(`${name}: ${item}`);
      } else if (value !== undefined) {
        lines.push(`${name}: ${value}`);
      }
    }
    upstream.write(`${lines.join("\r\n")}\r\n\r\n`);
    if (head.length > 0) upstream.write(head);
    socket.pipe(upstream).pipe(socket);
  });

  upstream.on("error", () => socket.destroy());
  socket.on("error", () => upstream.destroy());
});

server.on("clientError", (_error, socket) => {
  socket.end("HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n");
});

server.listen(listenPort, listenHost, () => {
  process.stdout.write(`Codex transparent proxy listening on ${listenHost}:${listenPort}\n`);
});

function shutdown() {
  server.close(() => process.exit(0));
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
