#!/usr/bin/env node

import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import fs from "node:fs";
import path, { extname } from "node:path";
import http from "node:http";
import https from "node:https";
import net from "node:net";
import os from "node:os";
import tls from "node:tls";
import { zstdCompressSync, zstdDecompressSync } from "node:zlib";
import { URL } from "node:url";

/**
 * Model routing is intentionally centralized here.  A model selection must
 * resolve provider, transport and upstream together for responses, compact
 * and resume requests.
 */
export const ROUTE_TABLE = Object.freeze([
  Object.freeze({
    id: "openai-native",
    provider: "openai",
    transport: "native",
    upstream: "gptNative",
    matches: (model) => /^(?:gpt-|codex-|o[134](?:-|$))/i.test(model),
  }),
  Object.freeze({
    id: "gemini-bridge",
    provider: "gemini",
    transport: "bridge",
    upstream: "cliProxy",
    matches: (model) => /^gemini-/i.test(model),
  }),
  Object.freeze({
    id: "deepseek-bridge",
    provider: "deepseek",
    transport: "bridge",
    upstream: "cliProxy",
    matches: (model) => /^deepseek-/i.test(model),
  }),
  Object.freeze({
    id: "claude-bridge",
    provider: "claude",
    transport: "bridge",
    upstream: "cliProxy",
    matches: (model) => /^claude-/i.test(model),
  }),
  Object.freeze({
    id: "glm-bridge",
    provider: "glm",
    transport: "bridge",
    upstream: "cliProxy",
    matches: (model) => /^glm-/i.test(model),
  }),
  Object.freeze({
    id: "thirdparty-bridge",
    provider: "thirdparty",
    transport: "bridge",
    upstream: "cliProxy",
    matches: (model) => /^(?:minimax|qwen-|kimi-|moonshot-|doubao-|baichuan-|yi-|llama-|mistral-)/i.test(model),
  }),
]);

const DEFAULTS = Object.freeze({
  listenHost: "127.0.0.1",
  listenPort: 8318,
  cliProxyBaseUrl: "http://127.0.0.1:8317",
  gptNativeBaseUrl: "https://chatgpt.com/backend-api/codex",
  authDir: `${process.env.HOME || process.env.USERPROFILE || os.homedir()}/.cli-proxy-api`,
  helper: `${process.env.HOME || process.env.USERPROFILE || os.homedir()}/.config/codex-cli-proxy/read-client-key.py`,
  helperPython: process.env.CODEX_BRIDGE_PYTHON || process.env.PYTHON || "python3",
  requestTimeoutMs: 120_000,
  disableGptWebSockets: true,
  antigravityRefreshLeadMs: 5 * 60 * 1000,
  antigravityRebalanceIntervalMs: 15 * 60 * 1000,
  providerSwitchCompactionMode: "fail_closed",
  providerSwitchStateMode: "fail_closed",
  providerStateTtlMs: 2 * 60 * 60 * 1000,
  providerStateMaxEntries: 4096,
});

function parsePort(value, fallback) {
  const port = Number.parseInt(String(value ?? fallback), 10);
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new Error(`Invalid port: ${value}`);
  }
  return port;
}

function parseDuration(value, fallback) {
  const duration = Number.parseInt(String(value ?? fallback), 10);
  if (!Number.isInteger(duration) || duration < 1) {
    throw new Error(`Invalid duration: ${value}`);
  }
  return duration;
}

function parseCompactionMode(value, fallback = DEFAULTS.providerSwitchCompactionMode) {
  const mode = String(value ?? fallback).trim().toLowerCase();
  if (mode !== "fail_closed" && mode !== "drop_foreign") {
    throw new Error(`Invalid provider switch compaction mode: ${value}`);
  }
  return mode;
}

function parseProviderSwitchStateMode(value, fallback = DEFAULTS.providerSwitchStateMode) {
  const mode = String(value ?? fallback).trim().toLowerCase();
  if (mode !== "fail_closed" && mode !== "drop_foreign") {
    throw new Error(`Invalid provider switch state mode: ${value}`);
  }
  return mode;
}

export function createConfig(env = process.env) {
  return {
    listenHost: env.CODEX_BRIDGE_LISTEN_HOST || DEFAULTS.listenHost,
    listenPort: parsePort(env.CODEX_BRIDGE_LISTEN_PORT, DEFAULTS.listenPort),
    cliProxyBaseUrl: env.CODEX_BRIDGE_CLIPROXY_BASE_URL || DEFAULTS.cliProxyBaseUrl,
    gptNativeBaseUrl: env.CODEX_BRIDGE_GPT_NATIVE_BASE_URL || DEFAULTS.gptNativeBaseUrl,
    authDir: env.CODEX_BRIDGE_AUTH_DIR || DEFAULTS.authDir,
    helper: env.CODEX_BRIDGE_HELPER || DEFAULTS.helper,
    helperPython: env.CODEX_BRIDGE_PYTHON || env.PYTHON || DEFAULTS.helperPython,
    requestTimeoutMs: parseDuration(env.CODEX_BRIDGE_REQUEST_TIMEOUT_MS, DEFAULTS.requestTimeoutMs),
    disableGptWebSockets: env.CODEX_BRIDGE_DISABLE_GPT_WS !== "false",
    antigravityRefreshLeadMs: parseDuration(
      env.CODEX_BRIDGE_ANTIGRAVITY_REFRESH_LEAD_MS,
      DEFAULTS.antigravityRefreshLeadMs,
    ),
    antigravityRebalanceIntervalMs: parseDuration(
      env.CODEX_BRIDGE_ANTIGRAVITY_REBALANCE_INTERVAL_MS,
      DEFAULTS.antigravityRebalanceIntervalMs,
    ),
    providerSwitchCompactionMode: parseCompactionMode(
      env.CODEX_BRIDGE_PROVIDER_SWITCH_COMPACTION_MODE,
    ),
    providerSwitchStateMode: parseProviderSwitchStateMode(
      env.CODEX_BRIDGE_PROVIDER_SWITCH_STATE_MODE,
    ),
    providerStateTtlMs: parseDuration(
      env.CODEX_BRIDGE_PROVIDER_STATE_TTL_MS,
      DEFAULTS.providerStateTtlMs,
    ),
    providerStateMaxEntries: parseDuration(
      env.CODEX_BRIDGE_PROVIDER_STATE_MAX_ENTRIES,
      DEFAULTS.providerStateMaxEntries,
    ),
  };
}

export function resolveRoute(model) {
  const normalized = String(model || "").trim();
  if (!normalized) return null;
  return ROUTE_TABLE.find((route) => route.matches(normalized)) || null;
}

const GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token";
const ANTIGRAVITY_CLIENT_ID =
  process.env.ANTIGRAVITY_CLIENT_ID ||
  Buffer.from("MTA3MTAwNjA2MDU5MS10bWhzc2luMmgyMWxjcmUyMzV2dG9sb2poNGc0MDNlcC5hcHBzLmdvb2dsZXVzZXJjb250ZW50LmNvbQ==", "base64").toString("utf-8");
const ANTIGRAVITY_CLIENT_SECRET =
  process.env.ANTIGRAVITY_CLIENT_SECRET ||
  Buffer.from("R09DU1BYLUs1OEZXUjQ4NkxkTEoxbUxCOHNYQzR6NnFEQWY=", "base64").toString("utf-8");

const inFlightRefreshes = new Map();

export async function requestGoogleTokenRefresh(refreshToken, options = {}) {
  const tokenUrl = new URL(options.tokenEndpoint || GOOGLE_TOKEN_ENDPOINT);
  const body = new URLSearchParams({
    client_id: options.clientId || ANTIGRAVITY_CLIENT_ID,
    client_secret: options.clientSecret || ANTIGRAVITY_CLIENT_SECRET,
    grant_type: "refresh_token",
    refresh_token: refreshToken,
  }).toString();

  const reqImpl = options.requestImpl || ((target, reqOpts, cb) => {
    return (target.protocol === "https:" ? https : http).request(target, reqOpts, cb);
  });

  return new Promise((resolve, reject) => {
    const req = reqImpl(
      tokenUrl,
      {
        method: "POST",
        headers: {
          "content-type": "application/x-www-form-urlencoded",
          "content-length": String(Buffer.byteLength(body)),
          "user-agent": "Go-http-client/2.0",
        },
      },
      (res) => {
        const chunks = [];
        res.on("data", (chunk) => chunks.push(Buffer.from(chunk)));
        res.on("end", () => {
          const raw = Buffer.concat(chunks).toString("utf8");
          if (res.statusCode < 200 || res.statusCode >= 300) {
            return reject(new Error(`Token refresh failed with status ${res.statusCode}: ${raw}`));
          }
          try {
            resolve(JSON.parse(raw));
          } catch (e) {
            reject(new Error(`Failed to parse token response: ${e.message}`));
          }
        });
      },
    );
    req.setTimeout(options.timeoutMs || 10_000, () => {
      req.destroy(new Error("Token refresh timeout"));
    });
    req.on("error", reject);
    req.end(body);
  });
}

export function isAuthExpiringSoon(authData, leadMs = 300_000, now = Date.now()) {
  if (!authData || typeof authData !== "object") return false;
  if (authData.expired) {
    const expiry = new Date(authData.expired).getTime();
    if (!Number.isNaN(expiry)) {
      return expiry <= now + leadMs;
    }
  }
  if (authData.timestamp && authData.expires_in) {
    const expiry = authData.timestamp + authData.expires_in * 1000;
    return expiry <= now + leadMs;
  }
  return false;
}

export async function refreshAntigravityAuthFile(filePath, options = {}) {
  if (!fs.existsSync(filePath)) return false;

  let raw;
  try {
    raw = fs.readFileSync(filePath, "utf8");
  } catch {
    return false;
  }

  let data;
  try {
    data = JSON.parse(raw);
  } catch {
    return false;
  }

  if (data?.type !== "antigravity" || !data?.refresh_token) {
    return false;
  }

  const leadMs = options.leadMs ?? (5 * 60 * 1000);
  if (!isAuthExpiringSoon(data, leadMs, options.now)) {
    return true;
  }

  if (inFlightRefreshes.has(filePath)) {
    return inFlightRefreshes.get(filePath);
  }

  const task = (async () => {
    let lastError = null;
    const maxRetries = options.maxRetries ?? 3;
    const retryDelayMs = options.retryDelayMs ?? 1000;

    for (let attempt = 1; attempt <= maxRetries; attempt++) {
      try {
        const tokenResp = await requestGoogleTokenRefresh(data.refresh_token, options);
        if (tokenResp?.access_token) {
          data.access_token = tokenResp.access_token;
          if (tokenResp.refresh_token) {
            data.refresh_token = tokenResp.refresh_token;
          }
          data.expires_in = tokenResp.expires_in || 3599;
          data.timestamp = Date.now();
          data.expired = new Date(Date.now() + data.expires_in * 1000).toISOString();
          data.disabled = false;

          fs.writeFileSync(filePath, JSON.stringify(data, null, 2), "utf8");

          if (options.log) {
            options.log(`[router] event=antigravity_auto_refreshed file=${filePath} expires_in=${data.expires_in}`);
          }
          return "refreshed";
        }
      } catch (err) {
        lastError = err;
        if (options.log) {
          options.log(`[router] event=antigravity_refresh_retry attempt=${attempt} error=${err.message}`);
        }
        if (attempt < maxRetries) {
          await new Promise((r) => setTimeout(r, retryDelayMs));
        }
      }
    }
    if (options.log) {
      options.log(`[router] event=antigravity_refresh_failed file=${filePath} error=${lastError?.message}`);
    }
    return false;
  })();

  inFlightRefreshes.set(filePath, task);
  try {
    return await task;
  } finally {
    inFlightRefreshes.delete(filePath);
  }
}

export async function ensureAntigravityAuthReady(authDir, options = {}) {
  if (!authDir || !fs.existsSync(authDir)) return false;
  try {
    const files = fs.readdirSync(authDir);
    const antigravityFiles = files
      .filter((f) => f.startsWith("antigravity-") && f.endsWith(".json"))
      .map((f) => path.join(authDir, f));

    if (antigravityFiles.length === 0) return true;
    const results = await Promise.all(antigravityFiles.map((f) => refreshAntigravityAuthFile(f, options)));
    if (results.some((r) => r === "refreshed")) {
      const syncDelayMs = options.syncDelayMs ?? 300;
      await new Promise((resolve) => setTimeout(resolve, syncDelayMs));
    }
    return true;
  } catch (err) {
    if (options.log) {
      options.log(`[router] event=antigravity_scan_error error=${err.message}`);
    }
    return false;
  }
}
function firstHeader(headers, names) {
  for (const name of names) {
    const value = headers[name] ?? headers[name.toLowerCase()];
    if (value !== undefined && value !== "") return Array.isArray(value) ? value[0] : value;
  }
  return undefined;
}

function parseRoutingHint(value) {
  if (!value) return undefined;
  const match = String(value).match(/(?:^|[;,\s])model=([^;,\s]+)/i);
  return match?.[1];
}

function parseJsonModel(body) {
  if (!body || body.length === 0) return undefined;
  const contentType = String(body.headers?.["content-type"] || "");
  if (body instanceof Buffer && contentType.includes("application/json")) {
    try {
      const json = JSON.parse(body.toString("utf8"));
      return json?.model || json?.request?.model;
    } catch {
      return undefined;
    }
  }
  return undefined;
}

export function extractModel(headers, bodyBuffer) {
  const hinted = parseRoutingHint(firstHeader(headers, ["x-codex-routing-hint"]));
  if (hinted) return hinted;
  const direct = firstHeader(headers, ["x-codex-model", "x-model"]);
  if (direct) return direct;
  if (bodyBuffer?.length) {
    try {
      const json = JSON.parse(bodyBuffer.toString("utf8"));
      return json?.model || json?.request?.model;
    } catch {
      // Codex may use zstd for the request body.  The routing hint above is
      // present on those requests; do not guess a provider when it is absent.
    }
  }
  return undefined;
}

function parseTurnMetadata(headers) {
  const raw = firstHeader(headers, ["x-codex-turn-metadata"]);
  if (!raw) return {};
  try {
    const metadata = JSON.parse(raw);
    return {
      operation: metadata.request_kind || metadata.operation,
      threadId: metadata.thread_id || metadata.threadId,
      sessionId: metadata.session_id || metadata.sessionId,
    };
  } catch {
    return {};
  }
}

export function classifyOperation(request) {
  const metadata = parseTurnMetadata(request.headers);
  if (metadata.operation) {
    const normalized = String(metadata.operation).toLowerCase();
    if (normalized.includes("compact")) return "compact";
    if (normalized.includes("resume")) return "resume";
    return "responses";
  }
  const path = String(request.url || "").toLowerCase();
  if (path.includes("compact")) return "compact";
  if (path.includes("resume")) return "resume";
  return "responses";
}

function safeId(value) {
  if (value === undefined || value === null || value === "") return "-";
  return String(value).replace(/[\r\n\t ]+/g, "_").slice(0, 160);
}

export function requestContext(request, model, route) {
  const metadata = parseTurnMetadata(request.headers);
  return {
    requestId:
      firstHeader(request.headers, ["x-request-id", "x-codex-request-id"]) || randomUUID(),
    threadId:
      metadata.threadId || firstHeader(request.headers, ["x-codex-thread-id", "x-thread-id"]),
    sessionId:
      metadata.sessionId || firstHeader(request.headers, ["x-codex-session-id", "x-session-id"]),
    model: model || "-",
    provider: route?.provider || "-",
    transport: route?.transport || "-",
    upstream: route?.upstream || "-",
    operation: classifyOperation(request),
  };
}

export function formatRouterLog(context, event = "route") {
  return [
    "[router]",
    `event=${safeId(event)}`,
    `request_id=${safeId(context.requestId)}`,
    `thread_id=${safeId(context.threadId)}`,
    `session_id=${safeId(context.sessionId)}`,
    `model=${safeId(context.model)}`,
    `provider=${safeId(context.provider)}`,
    `transport=${safeId(context.transport)}`,
    `upstream=${safeId(context.upstream)}`,
    `operation=${safeId(context.operation)}`,
  ].join(" ");
}

function helperCommand(config) {
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
    return [process.env.CODEX_BRIDGE_HELPER_CMD, ...extra, config.helper];
  }
  if (extname(config.helper).toLowerCase() === ".py") {
    return [config.helperPython, config.helper];
  }
  return [process.env.CODEX_BRIDGE_RUBY || "ruby", config.helper];
}

export function readClientKey(config) {
  const [command, ...args] = helperCommand(config);
  const value = execFileSync(command, args, {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
    timeout: 5_000,
  }).trim();
  if (!value) throw new Error("CLIProxyAPI client key helper returned no value");
  return value;
}

function hopByHopHeaders(headers) {
  const excluded = new Set([
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
  ]);
  return Object.fromEntries(
    Object.entries(headers).filter(([name]) => !excluded.has(name.toLowerCase())),
  );
}

function hostHeader(url) {
  return url.port ? `${url.hostname}:${url.port}` : url.hostname;
}

export function upstreamTarget(route, config, requestPath) {
  const base = new URL(route.upstream === "gptNative" ? config.gptNativeBaseUrl : config.cliProxyBaseUrl);
  const incoming = new URL(requestPath || "/", "http://bridge.invalid");
  const suffix = route.upstream === "gptNative"
    ? incoming.pathname.replace(/^\/v1(?=\/|$)/, "") || "/"
    : incoming.pathname;
  base.pathname = `${base.pathname.replace(/\/$/, "")}${suffix}`;
  base.search = incoming.search;
  return base;
}

function isForeignEncryptedContent(value, targetProvider) {
  if (typeof value !== "string" || value.length === 0) return false;
  const isGeminiCarrier = value.startsWith("cpa-gemini-responses-carrier-v1:");
  const isAntigravityCompaction = value.startsWith(ANTIGRAVITY_COMPACTION_PREFIX);
  if (targetProvider === "openai") return isGeminiCarrier || isAntigravityCompaction;
  if (targetProvider === "gemini" || targetProvider === "claude") {
    return !isGeminiCarrier && !isAntigravityCompaction;
  }
  return !isGeminiCarrier && !isAntigravityCompaction;
}

function containsForeignEncryptedContent(value, targetProvider) {
  if (Array.isArray(value)) return value.some((item) => containsForeignEncryptedContent(item, targetProvider));
  if (!value || typeof value !== "object") return false;
  if (isForeignEncryptedContent(value.encrypted_content, targetProvider)) return true;
  return Object.values(value).some((item) => containsForeignEncryptedContent(item, targetProvider));
}

function scrubForeignEncryptedContent(value, targetProvider) {
  if (Array.isArray(value)) {
    return value
      .filter((item) => !(item?.type === "reasoning" && containsForeignEncryptedContent(item, targetProvider)))
      .map((item) => scrubForeignEncryptedContent(item, targetProvider));
  }
  if (!value || typeof value !== "object") return value;
  const result = {};
  for (const [key, item] of Object.entries(value)) {
    if (key === "encrypted_content" && isForeignEncryptedContent(item, targetProvider)) continue;
    result[key] = scrubForeignEncryptedContent(item, targetProvider);
  }
  return result;
}

const ANTIGRAVITY_COMPACTION_PREFIX = "cpa-ag-compact-v1:";
const GEMINI_RESPONSES_CARRIER_PREFIX = "cpa-gemini-responses-carrier-v1:";

export function compactionProviderFamily(routeProvider) {
  if (routeProvider === "openai") return "openai";
  if (routeProvider === "gemini" || routeProvider === "claude") return "antigravity";
  return null;
}

export function classifyCompactionCapsule(item) {
  if (!item || typeof item !== "object" || item.type !== "compaction") return null;
  let encryptedContent;
  const visit = (value) => {
    if (typeof encryptedContent === "string") return;
    if (typeof value === "string") return;
    if (Array.isArray(value)) {
      for (const nested of value) visit(nested);
      return;
    }
    if (!value || typeof value !== "object") return;
    if (typeof value.encrypted_content === "string") {
      encryptedContent = value.encrypted_content;
      return;
    }
    for (const nested of Object.values(value)) visit(nested);
  };
  visit(item);

  if (!encryptedContent) {
    return { family: "unknown", format: "missing", prefix: "" };
  }
  if (encryptedContent.startsWith(ANTIGRAVITY_COMPACTION_PREFIX)) {
    return { family: "antigravity", format: "antigravity", prefix: ANTIGRAVITY_COMPACTION_PREFIX };
  }
  if (encryptedContent.startsWith(GEMINI_RESPONSES_CARRIER_PREFIX)) {
    return { family: "gemini", format: "gemini_carrier", prefix: GEMINI_RESPONSES_CARRIER_PREFIX };
  }
  if (encryptedContent.startsWith("gAAAA")) {
    return { family: "openai", format: "openai", prefix: "gAAAA" };
  }
  return { family: "unknown", format: "unknown", prefix: encryptedContent.slice(0, 32) };
}

function findCompactionItems(value, items = []) {
  if (Array.isArray(value)) {
    for (const item of value) findCompactionItems(item, items);
    return items;
  }
  if (!value || typeof value !== "object") return items;
  if (value.type === "compaction") items.push(value);
  for (const item of Object.values(value)) findCompactionItems(item, items);
  return items;
}

function removeIncompatibleCompactionItems(value, targetFamily) {
  if (value && typeof value === "object" && !Array.isArray(value) && value.type === "compaction") {
    return classifyCompactionCapsule(value).family === targetFamily ? value : undefined;
  }
  if (Array.isArray(value)) {
    return value
      .map((item) => removeIncompatibleCompactionItems(item, targetFamily))
      .filter((item) => item !== undefined);
  }
  if (!value || typeof value !== "object") return value;
  const result = {};
  for (const [key, item] of Object.entries(value)) {
    const sanitized = removeIncompatibleCompactionItems(item, targetFamily);
    if (sanitized !== undefined) result[key] = sanitized;
  }
  return result;
}

export function inspectCompactionForRoute(payload, route) {
  const items = findCompactionItems(payload);
  const targetFamily = compactionProviderFamily(route?.provider);
  if (items.length === 0 || !targetFamily) {
    return { targetFamily, items: [], compatible: true, incompatible: [] };
  }
  const classified = items.map((item) => ({ item, capsule: classifyCompactionCapsule(item) }));
  const incompatible = classified.filter(({ capsule }) => capsule.family !== targetFamily);
  return {
    targetFamily,
    items: classified,
    compatible: incompatible.length === 0,
    incompatible,
  };
}

const BLOCKED_CODEX_IDENTITY = "You are Codex, an agent based on GPT-5.";
const SAFE_CODEX_IDENTITY = "You are a helpful AI coding assistant.";

// The router cannot create a Codex task itself.  Return stable, machine-readable
// choices so the Skill/UI can ask the user what to do after a cross-provider
// compaction conflict instead of silently dropping context or retrying forever.
export const PROVIDER_SWITCH_CHOICES = Object.freeze([
  Object.freeze({
    id: "handoff",
    label: "Start a new target-model task with a plain-text summary",
    label_zh: "新建目标模型任务并迁移摘要",
    recommended: true,
  }),
  Object.freeze({
    id: "cancel",
    label: "Cancel the switch and continue with the source-model task",
    label_zh: "取消切换并继续原模型任务",
    recommended: false,
  }),
]);

const PROVIDER_RESPONSE_ID = /^(?:rs|resp|msg|fc|fco)_/i;
const PROVIDER_STATE_TYPES = new Set([
  "reasoning",
  "function_call",
  "function_call_output",
  "item_reference",
]);

function providerStateId(value) {
  if (typeof value !== "string") return null;
  return PROVIDER_RESPONSE_ID.test(value) ? value : null;
}

function findProviderResponseState(value, path = "$", items = []) {
  if (Array.isArray(value)) {
    value.forEach((item, index) => findProviderResponseState(item, `${path}[${index}]`, items));
    return items;
  }
  if (!value || typeof value !== "object") return items;

  for (const [key, nested] of Object.entries(value)) {
    if (key === "previous_response_id" && typeof nested === "string") {
      items.push({ path: `${path}.${key}`, kind: key, id: nested });
    }
  }

  const type = typeof value.type === "string" ? value.type : "";
  const id = providerStateId(value.id) || providerStateId(value.item_id) || providerStateId(value.response_id);
  if (id && (PROVIDER_STATE_TYPES.has(type) || type === "" || type === "response_item") && type !== "item_reference") {
    items.push({ path, kind: type || "id", id, type: type || null });
  }
  if (type === "item_reference" && (value.item_id || value.id)) {
    items.push({
      path,
      kind: type,
      id: String(value.item_id || value.id),
      type,
    });
  }

  for (const [key, nested] of Object.entries(value)) {
    findProviderResponseState(nested, `${path}.${key}`, items);
  }
  return items;
}

/**
 * Detects response state that is not portable between provider translators.
 * The router only applies this guard when a session/thread has already been
 * observed on another provider; ordinary new requests remain untouched.
 */
export function inspectProviderSwitchState(payload, route, previousProvider) {
  const targetProvider = route?.provider || null;
  const stateItems = findProviderResponseState(payload);
  const sourceProvider = previousProvider || null;
  const compatible = !sourceProvider || !targetProvider || sourceProvider === targetProvider || stateItems.length === 0;
  return {
    sourceProvider,
    targetProvider,
    compatible,
    stateItems,
    reasons: stateItems.map(({ kind, id, path }) => `${kind}:${id || path}`),
  };
}

function stripProviderResponseState(value) {
  let dropped = 0;
  const visit = (item) => {
    if (Array.isArray(item)) {
      return item
        .map((nested) => visit(nested))
        .filter((nested) => nested !== undefined);
    }
    if (!item || typeof item !== "object") return item;
    if (typeof item.type === "string" && PROVIDER_STATE_TYPES.has(item.type)) {
      const id = providerStateId(item.id) || providerStateId(item.item_id) || providerStateId(item.response_id);
      if (item.type === "item_reference" || id) {
        dropped += 1;
        return undefined;
      }
    }
    const result = {};
    for (const [key, nested] of Object.entries(item)) {
      if (key === "previous_response_id") {
        dropped += 1;
        continue;
      }
      result[key] = visit(nested);
    }
    return result;
  };
  return { value: visit(value), dropped };
}

export function removeProviderScopedResponseState(payload) {
  return stripProviderResponseState(payload);
}

function rewriteBlockedCodexIdentity(value) {
  let replacements = 0;
  const visit = (item) => {
    if (typeof item === "string") {
      const count = item.split(BLOCKED_CODEX_IDENTITY).length - 1;
      if (count === 0) return item;
      replacements += count;
      return item.replaceAll(BLOCKED_CODEX_IDENTITY, SAFE_CODEX_IDENTITY);
    }
    if (Array.isArray(item)) return item.map(visit);
    if (!item || typeof item !== "object") return item;
    return Object.fromEntries(Object.entries(item).map(([key, nested]) => [key, visit(nested)]));
  };
  return { value: visit(value), replacements };
}

export function sanitizeBodyForRoute(bodyBuffer, requestHeaders, route, options = {}) {
  const contentType = String(requestHeaders["content-type"] || "");
  if (!bodyBuffer?.length || !contentType.includes("application/json")) {
    return {
      buffer: bodyBuffer,
      changed: false,
      promptRewriteCount: 0,
      compactionGuard: null,
      compactionDropped: 0,
      providerSwitchGuard: null,
      providerStateDropped: 0,
    };
  }
  const encoding = String(requestHeaders["content-encoding"] || "").toLowerCase();
  let decoded = bodyBuffer;
  try {
    if (encoding === "zstd") decoded = zstdDecompressSync(bodyBuffer);
    const payload = JSON.parse(decoded.toString("utf8"));
    let sanitized = payload;
    let changed = false;
    let promptRewriteCount = 0;
    const compactionInspection = inspectCompactionForRoute(sanitized, route);
    const compactionMode = options.providerSwitchCompactionMode || "fail_closed";
    const providerStateMode = options.providerSwitchStateMode || "fail_closed";
    let compactionGuard = null;
    let compactionDropped = 0;
    let providerSwitchGuard = null;
    let providerStateDropped = 0;

    const stateInspection = inspectProviderSwitchState(
      sanitized,
      route,
      options.previousProvider,
    );
    if (!stateInspection.compatible) {
      if (providerStateMode === "drop_foreign") {
        const stripped = removeProviderScopedResponseState(sanitized);
        sanitized = stripped.value;
        providerStateDropped = stripped.dropped;
        providerSwitchGuard = {
          action: "dropped",
          sourceProvider: stateInspection.sourceProvider,
          targetProvider: stateInspection.targetProvider,
          reasons: stateInspection.reasons,
        };
        changed ||= providerStateDropped > 0;
      } else {
        providerSwitchGuard = {
          action: "blocked",
          sourceProvider: stateInspection.sourceProvider,
          targetProvider: stateInspection.targetProvider,
          reasons: stateInspection.reasons,
        };
        return {
          buffer: bodyBuffer,
          changed: false,
          promptRewriteCount: 0,
          compactionGuard: null,
          compactionDropped: 0,
          providerSwitchGuard,
          providerStateDropped: 0,
        };
      }
    }
    if (!compactionInspection.compatible) {
      const incompatible = compactionInspection.incompatible;
      if (compactionMode === "drop_foreign") {
        sanitized = removeIncompatibleCompactionItems(sanitized, compactionInspection.targetFamily);
        if (sanitized && typeof sanitized === "object" && !Array.isArray(sanitized)) {
          delete sanitized.previous_response_id;
        }
        compactionDropped = incompatible.length;
        compactionGuard = {
          action: "dropped",
          targetFamily: compactionInspection.targetFamily,
          incompatible: incompatible.map(({ capsule }) => capsule),
        };
        changed = true;
      } else {
        compactionGuard = {
          action: "blocked",
          targetFamily: compactionInspection.targetFamily,
          incompatible: incompatible.map(({ capsule }) => capsule),
        };
        return {
          buffer: bodyBuffer,
          changed: false,
          promptRewriteCount: 0,
          compactionGuard,
          compactionDropped: 0,
          providerSwitchGuard,
          providerStateDropped,
        };
      }
    }
    if (containsForeignEncryptedContent(sanitized, route.provider)) {
      sanitized = scrubForeignEncryptedContent(sanitized, route.provider);
      if (sanitized && typeof sanitized === "object" && !Array.isArray(sanitized)) {
        delete sanitized.previous_response_id;
      }
      changed = true;
    }
    if (route.provider === "gemini" || route.provider === "claude") {
      const rewritten = rewriteBlockedCodexIdentity(sanitized);
      sanitized = rewritten.value;
      promptRewriteCount = rewritten.replacements;
      changed ||= promptRewriteCount > 0;
    }
    if (!changed) {
      return {
        buffer: bodyBuffer,
        changed: false,
        promptRewriteCount: 0,
        compactionGuard,
        compactionDropped,
        providerSwitchGuard,
        providerStateDropped,
      };
    }
    let output = Buffer.from(JSON.stringify(sanitized));
    if (encoding === "zstd") output = zstdCompressSync(output);
    return {
      buffer: output,
      changed: true,
      promptRewriteCount,
      compactionGuard,
      compactionDropped,
      providerSwitchGuard,
      providerStateDropped,
    };
  } catch {
    // Never guess or corrupt an opaque request.  The route remains explicit;
    // Codex/upstream will return the original protocol error if it is opaque.
    return {
      buffer: bodyBuffer,
      changed: false,
      promptRewriteCount: 0,
      compactionGuard: null,
      compactionDropped: 0,
      providerSwitchGuard: null,
      providerStateDropped: 0,
    };
  }
}

function headersForRoute(route, request, config, target) {
  const headers = hopByHopHeaders(request.headers);
  headers.host = hostHeader(target);
  if (route.upstream === "cliProxy") {
    headers.authorization = `Bearer ${readClientKey(config)}`;
  }
  return headers;
}

function collectBody(request) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    request.on("data", (chunk) => chunks.push(Buffer.from(chunk)));
    request.on("end", () => resolve(Buffer.concat(chunks)));
    request.on("error", reject);
  });
}

function responseJson(response, status, payload) {
  if (response.headersSent) {
    response.destroy();
    return;
  }
  response.writeHead(status, { "content-type": "application/json" });
  response.end(JSON.stringify(payload));
}

const MAX_CLASSIFIED_ERROR_BYTES = 64 * 1024;

function readJsonIfPresent(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch {
    return null;
  }
}

/**
 * Only turn an Antigravity pool exhaustion into an action-required error when
 * every enabled credential has a current, model-relevant cooldown.  The 503's
 * last upstream error is historical and is not enough to classify the pool.
 * Never return or log credential contents, account emails, or validation URLs.
 */
export function classifyAntigravityAuthUnavailable(status, bodyBuffer, authDir, model, now = Date.now()) {
  if (status !== 503 || !/^gemini-/i.test(String(model || ""))) return null;
  let upstream;
  try {
    upstream = JSON.parse(bodyBuffer.toString("utf8"));
  } catch {
    return null;
  }
  const message = upstream?.error?.message;
  if (typeof message !== "string" ||
      !/^auth_unavailable:\s*no auth available\b/i.test(message) ||
      !/providers=antigravity(?:[,;)\s]|$)/i.test(message) ||
      !message.includes(`model=${model}`)) return null;

  let files;
  try {
    files = fs.readdirSync(authDir);
  } catch {
    return null;
  }
  const statusByAuthId = new Map();
  for (const name of files.filter((entry) => entry.endsWith(".cds"))) {
    const data = readJsonIfPresent(path.join(authDir, name));
    if (data?.provider === "antigravity" && typeof data.auth_id === "string") {
      statusByAuthId.set(data.auth_id, data.records);
    }
  }
  let enabled = 0;
  let validation = 0;
  let quota = 0;
  let nextCheck = Infinity;
  for (const name of files.filter((entry) => entry.endsWith(".json"))) {
    const auth = readJsonIfPresent(path.join(authDir, name));
    if (auth?.type !== "antigravity" || auth.disabled === true) continue;
    enabled += 1;
    const records = statusByAuthId.get(name);
    if (!Array.isArray(records)) return null;
    const blocked = records.filter((record) =>
      (record?.model === model || !record?.model) &&
      record?.status === "cooling" &&
      Date.parse(record.next_retry_after) > now
    );
    if (blocked.length === 0) return null;
    nextCheck = Math.min(nextCheck, ...blocked.map((record) => Date.parse(record.next_retry_after)));
    if (blocked.some((record) =>
      record.last_error?.http_status === 403 &&
      /VALIDATION_REQUIRED/i.test(`${record.reason || ""} ${record.last_error?.message || ""}`)
    )) {
      validation += 1;
    } else if (blocked.some((record) =>
      record.last_error?.http_status === 429 || record.quota?.exceeded === true
    )) {
      quota += 1;
    } else {
      return null;
    }
  }
  if (enabled === 0 || validation === 0 || validation + quota !== enabled) return null;
  return { enabled, validation, quota, nextCheckAt: new Date(nextCheck).toISOString() };
}

function forwardWithPoolFailureGuard(upstream, response, route, context, config, log) {
  const status = upstream.statusCode || 502;
  if (route.provider !== "gemini" || status !== 503 ||
      upstream.headers["content-encoding"] ||
      !String(upstream.headers["content-type"] || "").includes("application/json")) {
    response.writeHead(status, upstream.headers);
    upstream.pipe(response);
    return;
  }
  const chunks = [];
  let size = 0;
  const onData = (chunk) => {
    size += chunk.length;
    chunks.push(chunk);
    if (size > MAX_CLASSIFIED_ERROR_BYTES) {
      upstream.off("data", onData);
      upstream.off("end", onEnd);
      response.writeHead(status, upstream.headers);
      response.write(Buffer.concat(chunks));
      upstream.pipe(response);
    }
  };
  const onEnd = () => {
    const raw = Buffer.concat(chunks);
    const pool = classifyAntigravityAuthUnavailable(status, raw, config.authDir, context.model);
    if (!pool) {
      response.writeHead(status, upstream.headers);
      response.end(raw);
      return;
    }
    log(`${formatRouterLog(context, "antigravity_pool_action_required")} validation=${pool.validation} quota=${pool.quota}`);
    // Codex 0.155.0-alpha retries 403/409/422/424 stream failures six times;
    // a model-scoped 400 with an explicit code terminates after one request.
    responseJson(response, 400, {
      error: {
        type: "upstream_account_action_required",
        code: "antigravity_account_action_required",
        message: `No eligible Antigravity account for this Gemini model. ${pool.quota > 0
          ? "Google account verification is required for some accounts; the remaining accounts are in quota cooldown."
          : "Google account verification is required for every enabled account."} Repeating OAuth login or this request will not resolve it. Verify the affected Google accounts, or explicitly choose another model in a new task with a plain-text summary. The router will not switch providers silently.`,
      },
      retryable: false,
      action_required: true,
      handoff_available: true,
      handoff_mode: "new_task_with_plain_text_summary",
      next_check_at: pool.nextCheckAt,
      request_id: context.requestId,
    });
  };
  upstream.on("data", onData);
  upstream.once("end", onEnd);
  upstream.once("error", (error) => {
    log(`${formatRouterLog(context, "upstream_response_error")} cause=${safeId(error?.code || "stream_error")}`);
    responseJson(response, 502, publicError(error, context));
  });
}

function publicError(error, context) {
  return {
    error: "upstream_unavailable",
    message: `${context.provider} upstream unavailable`,
    request_id: context.requestId,
    provider: context.provider,
    model: context.model,
    cause: error?.code || error?.message || "request_failed",
  };
}

function requestModule(target) {
  return target.protocol === "https:" ? https : http;
}

export function createRouterServer(options = {}) {
  const config = { ...createConfig(), ...options.config };
  const log = options.log || ((line) => process.stderr.write(`${line}\n`));
  const providerStateBySession = new Map();
  const requestImpl = options.requestImpl || ((target, requestOptions, callback) => {
    return requestModule(target).request(target, requestOptions, callback);
  });

  function providerStateKey(context) {
    const id = context.threadId || context.sessionId;
    return id ? String(id) : null;
  }

  function previousProviderFor(context) {
    const key = providerStateKey(context);
    if (!key) return { key: null, provider: null };
    const current = providerStateBySession.get(key);
    if (!current || Date.now() - current.seenAt > config.providerStateTtlMs) {
      if (current) providerStateBySession.delete(key);
      return { key, provider: null };
    }
    return { key, provider: current.provider };
  }

  function rememberProvider(key, provider) {
    if (!key || !provider) return;
    providerStateBySession.delete(key);
    providerStateBySession.set(key, { provider, seenAt: Date.now() });
    while (providerStateBySession.size > config.providerStateMaxEntries) {
      const oldest = providerStateBySession.keys().next().value;
      if (oldest === undefined) break;
      providerStateBySession.delete(oldest);
    }
  }

  async function handleRequest(request, response) {
    if (request.url === "/__codex_bridge_health") {
      responseJson(response, 200, {
        status: "ok",
        router: "model-aware",
        routes: ROUTE_TABLE.map(({ id, provider, transport, upstream }) => ({
          id,
          provider,
          transport,
          upstream,
        })),
        upstreams: {
          gptNative: config.gptNativeBaseUrl,
          cliProxy: config.cliProxyBaseUrl,
        },
        providerSwitchCompactionMode: config.providerSwitchCompactionMode,
        providerSwitchStateMode: config.providerSwitchStateMode,
      });
      return;
    }

    if (request.url.startsWith("/__sse_shim/")) {
      const rawTarget = request.url.slice("/__sse_shim/".length);
      let targetUrl;
      try {
        targetUrl = new URL(rawTarget.startsWith("http") ? rawTarget : `https://${rawTarget}`);
      } catch (err) {
        responseJson(response, 400, { error: "invalid_shim_target", message: err.message });
        return;
      }

      const headers = hopByHopHeaders(request.headers);
      headers.host = hostHeader(targetUrl);
      delete headers["content-length"];

      const client = targetUrl.protocol === "https:" ? https : http;
      const upstream = client.request(targetUrl, {
        method: request.method,
        headers,
      }, (upRes) => {
        response.writeHead(upRes.statusCode || 502, upRes.headers);
        let sawDone = false;
        const isSse = String(upRes.headers["content-type"] || "").includes("text/event-stream");

        upRes.on("data", (chunk) => {
          const str = chunk.toString("utf8");
          if (str.includes("[DONE]")) sawDone = true;
          response.write(chunk);
        });

        upRes.on("end", () => {
          if (isSse && !sawDone) {
            response.write("\ndata: [DONE]\n\n");
          }
          response.end();
        });
      });

      upstream.on("error", (err) => {
        if (!response.headersSent) {
          responseJson(response, 502, { error: "upstream_shim_error", message: err.message });
        } else {
          response.destroy();
        }
      });

      request.pipe(upstream);
      return;
    }

    let body;
    try {
      body = await collectBody(request);
    } catch (error) {
      responseJson(response, 400, { error: "request_body_unreadable", cause: error.message });
      return;
    }

    const model = extractModel(request.headers, body);
    const route = resolveRoute(model);
    const context = requestContext(request, model, route);
    log(formatRouterLog(context));

    if (!route) {
      responseJson(response, 400, {
        error: "model_route_not_found",
        message: "Model is required and must match an explicit router route",
        request_id: context.requestId,
      });
      return;
    }

    if (route.provider === "gemini" || route.provider === "claude") {
      try {
        await ensureAntigravityAuthReady(config.authDir, {
          tokenEndpoint: options.tokenEndpoint,
          clientId: options.clientId,
          clientSecret: options.clientSecret,
          leadMs: config.antigravityRefreshLeadMs,
          log,
          requestImpl,
        });
      } catch (err) {
        log(`[router] event=antigravity_preflight_error error=${err?.message || err}`);
      }
    }

    let target;
    let headers;
    try {
      target = upstreamTarget(route, config, request.url);
      headers = headersForRoute(route, request, config, target);
    } catch (error) {
      log(formatRouterLog(context, "route_error"));
      responseJson(response, 502, publicError(error, context));
      return;
    }

    const state = previousProviderFor(context);
    const prepared = sanitizeBodyForRoute(body, request.headers, route, {
      providerSwitchCompactionMode: config.providerSwitchCompactionMode,
      providerSwitchStateMode: config.providerSwitchStateMode,
      previousProvider: state.provider,
    });
    if (prepared.providerSwitchGuard?.action === "blocked") {
      log(`${formatRouterLog(context, "provider_switch_state_blocked")} source_provider=${safeId(prepared.providerSwitchGuard.sourceProvider)} target_provider=${safeId(prepared.providerSwitchGuard.targetProvider)} reasons=${safeId(prepared.providerSwitchGuard.reasons.join(","))}`);
      responseJson(response, 409, {
        error: "provider_switch_state_conflict",
        message: "This conversation contains response items from another model provider. Choose handoff to create a new target-model task with a plain-text summary, or cancel the switch.",
        retryable: false,
        handoff_required: true,
        choice_required: true,
        choices: PROVIDER_SWITCH_CHOICES,
        handoff_mode: "new_task_with_plain_text_summary",
        source_thread_id: context.threadId || null,
        source_provider: prepared.providerSwitchGuard.sourceProvider,
        target_provider: prepared.providerSwitchGuard.targetProvider,
        target_model: context.model,
        reasons: prepared.providerSwitchGuard.reasons,
        request_id: context.requestId,
      });
      return;
    }
    if (prepared.compactionGuard?.action === "blocked") {
      const formats = prepared.compactionGuard.incompatible
        .map((capsule) => capsule.format)
        .join(",");
      log(`${formatRouterLog(context, "provider_switch_compaction_blocked")} target_family=${prepared.compactionGuard.targetFamily} formats=${formats || "unknown"}`);
      responseJson(response, 409, {
        error: "provider_switch_compaction_conflict",
        message: "This conversation contains a compaction state from another model provider. Start a new conversation for the selected model and paste a summary of the previous context.",
        retryable: false,
        handoff_required: true,
        choice_required: true,
        choices: PROVIDER_SWITCH_CHOICES,
        handoff_mode: "new_task_with_plain_text_summary",
        source_thread_id: context.threadId || null,
        target_model: context.model,
        request_id: context.requestId,
        provider: context.provider,
        model: context.model,
        target_family: prepared.compactionGuard.targetFamily,
      });
      return;
    }
    if (prepared.compactionGuard?.action === "dropped") {
      const formats = prepared.compactionGuard.incompatible
        .map((capsule) => capsule.format)
        .join(",");
      log(`${formatRouterLog(context, "provider_switch_compaction_dropped")} target_family=${prepared.compactionGuard.targetFamily} formats=${formats || "unknown"} count=${prepared.compactionDropped}`);
    }
    if (prepared.providerSwitchGuard?.action === "dropped") {
      log(`${formatRouterLog(context, "provider_switch_state_dropped")} source_provider=${safeId(prepared.providerSwitchGuard.sourceProvider)} target_provider=${safeId(prepared.providerSwitchGuard.targetProvider)} count=${prepared.providerStateDropped}`);
    }
    if (prepared.promptRewriteCount > 0) {
      log(`${formatRouterLog(context, "antigravity_prompt_rewrite")} replacements=${prepared.promptRewriteCount}`);
    }
    if (prepared.changed) {
      headers["content-length"] = String(prepared.buffer.length);
    }
    const requestOptions = {
      method: request.method,
      path: `${target.pathname}${target.search}`,
      headers,
    };
    const upstreamRequest = requestImpl(target, requestOptions, (upstreamResponse) => {
      if (upstreamResponse.statusCode === 429 && (route.provider === "gemini" || route.provider === "claude")) {
        scheduleFastRebalanceOnQuota(log);
      }
      forwardWithPoolFailureGuard(upstreamResponse, response, route, context, config, log);
    });
    upstreamRequest.setTimeout(config.requestTimeoutMs, () => upstreamRequest.destroy(new Error("upstream_timeout")));
    upstreamRequest.on("error", (error) => {
      log(formatRouterLog(context, "upstream_error"));
      responseJson(response, 502, publicError(error, context));
    });
    rememberProvider(state.key, route.provider);
    upstreamRequest.end(prepared.buffer);
  }

  const server = http.createServer((request, response) => {
    void handleRequest(request, response);
  });

  server.on("upgrade", (request, socket, head) => {
    const model = parseRoutingHint(firstHeader(request.headers, ["x-codex-routing-hint"])) ||
      firstHeader(request.headers, ["x-codex-model", "x-model"]);
    const route = resolveRoute(model);
    const context = requestContext(request, model, route);
    log(formatRouterLog(context, "upgrade"));
    if (!route) {
      socket.end("HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n");
      return;
    }
    if (route.upstream === "gptNative" && config.disableGptWebSockets) {
      log(formatRouterLog(context, "gpt_ws_disabled"));
      socket.end("HTTP/1.1 426 Upgrade Required\r\nConnection: close\r\n\r\n");
      return;
    }
    if (route.provider === "gemini" || route.provider === "claude") {
      log(formatRouterLog(context, "antigravity_ws_disabled_for_prompt_rewrite"));
      socket.end("HTTP/1.1 426 Upgrade Required\r\nConnection: close\r\n\r\n");
      return;
    }
    if (route.provider === "gemini" || route.provider === "claude") {
      try {
        void ensureAntigravityAuthReady(config.authDir, {
          tokenEndpoint: options.tokenEndpoint,
          clientId: options.clientId,
          clientSecret: options.clientSecret,
          leadMs: config.antigravityRefreshLeadMs,
          log,
        });
      } catch (err) {
        log(`[router] event=antigravity_preflight_upgrade_error error=${err?.message || err}`);
      }
    }
    let target;
    try {
      target = upstreamTarget(route, config, request.url);
      const headers = headersForRoute(route, request, config, target);
      const port = target.port ? Number(target.port) : target.protocol === "https:" ? 443 : 80;
      const connection = target.protocol === "https:"
        ? tls.connect({ host: target.hostname, port, servername: target.hostname })
        : net.connect(port, target.hostname);
      const connectedEvent = target.protocol === "https:" ? "secureConnect" : "connect";
      connection.once(connectedEvent, () => {
        const lines = [`${request.method} ${target.pathname}${target.search} HTTP/${request.httpVersion}`];
        for (const [name, value] of Object.entries({ ...headers, connection: "Upgrade", upgrade: "websocket" })) {
          lines.push(`${name}: ${value}`);
        }
        lines.push("", "");
        connection.write(lines.join("\r\n"));
        if (head?.length) connection.write(head);
        socket.pipe(connection).pipe(socket);
      });
      connection.on("error", () => socket.destroy());
    } catch {
      socket.end("HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n");
    }
  });

  return server;
}


export function autoSyncCatalogOnStartup(log = () => {}) {
  try {
    const scriptPath = path.join(
      process.env.HOME || process.env.USERPROFILE || os.homedir(),
      ".codex",
      "skills",
      "codex-autoheal-bridge",
      "scripts",
      "auto_sync_official.py"
    );
    if (!fs.existsSync(scriptPath)) return;
    const pythonBin = process.env.CODEX_BRIDGE_PYTHON || process.env.PYTHON || "python3";
    const out = execFileSync(pythonBin, [scriptPath], { encoding: "utf8", timeout: 8000 });
    log(`[router] event=auto_sync_catalog result=${out.trim().replace(/\s+/g, " ")}`);
  } catch (err) {
    log(`[router] event=auto_sync_catalog_error error=${err?.message || err}`);
  }
}

export function watchNativeModelsCache(log = () => {}) {
  try {
    const nativeCache = path.join(
      process.env.HOME || process.env.USERPROFILE || os.homedir(),
      ".codex",
      "models_cache.json"
    );
    if (!fs.existsSync(nativeCache)) return;
    let debounceTimer = null;
    fs.watch(nativeCache, (eventType) => {
      if (debounceTimer) clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => {
        log(`[router] event=native_cache_changed event_type=${eventType}`);
        autoSyncCatalogOnStartup(log);
      }, 3000);
    });
  } catch (err) {
    log(`[router] event=watch_native_cache_error error=${err?.message || err}`);
  }
}

let fastRebalanceTimer = null;
export function scheduleFastRebalanceOnQuota(log = () => {}) {
  if (fastRebalanceTimer) return;
  fastRebalanceTimer = setTimeout(() => {
    fastRebalanceTimer = null;
    log("[router] event=trigger_fast_rebalance_on_quota");
    autoRebalanceAntigravityPool(log);
  }, 1500);
  if (fastRebalanceTimer.unref) fastRebalanceTimer.unref();
}

export function autoRebalanceAntigravityPool(log = () => {}) {
  try {
    const scriptPath = path.join(
      process.env.HOME || process.env.USERPROFILE || os.homedir(),
      ".codex",
      "skills",
      "codex-autoheal-bridge",
      "scripts",
      "antigravity_pool.py"
    );
    if (!fs.existsSync(scriptPath)) return;
    const pythonBin = process.env.CODEX_BRIDGE_PYTHON || process.env.PYTHON || "python3";
    const out = execFileSync(pythonBin, [scriptPath, "rebalance", "--apply"], {
      encoding: "utf8",
      timeout: 10000,
    });
    const summary = out
      .trim()
      .split("\n")
      .filter((l) => l.includes("updated=") || l.includes("rebalance complete"))
      .join("; ");
    log(`[router] event=auto_rebalance_antigravity ${summary || "pool up to date"}`);
  } catch (err) {
    log(`[router] event=auto_rebalance_antigravity_error error=${err?.message || err}`);
  }
}

export function startPeriodicAntigravityRebalance(intervalMs = 15 * 60 * 1000, log = () => {}) {
  const timer = setInterval(() => {
    autoRebalanceAntigravityPool(log);
  }, intervalMs);
  if (timer.unref) timer.unref();
  return timer;
}

export function startRouter(options = {}) {
  const config = options.config || createConfig();
  const server = options.server || createRouterServer({ ...options, config });
  server.listen(config.listenPort, config.listenHost, () => {
    process.stdout.write(
      `Codex model router listening on http://${config.listenHost}:${config.listenPort}/v1\n`,
    );
    try {
      const logFn = options.log || ((msg) => process.stdout.write(`${msg}\n`));
      autoSyncCatalogOnStartup(logFn);
      watchNativeModelsCache(logFn);
      autoRebalanceAntigravityPool(logFn);
      startPeriodicAntigravityRebalance(config.antigravityRebalanceIntervalMs, logFn);
    } catch (_) {}
  });
  const shutdown = () => server.close(() => process.exit(0));
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
  return server;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  startRouter();
}
