import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { Readable } from "node:stream";
import test from "node:test";

import {
  classifyCompactionCapsule,
  classifyAntigravityAuthUnavailable,
  createConfig,
  createRouterServer,
  inspectCompactionForRoute,
  inspectProviderSwitchState,
  isExternalMode,
  MetricsCollector,
  PROVIDER_SWITCH_CHOICES,
  removeProviderScopedResponseState,
  resolveRoute,
  sanitizeBodyForRoute,
  SlidingWindow,
  stripInternalHeaders,
} from "../scripts/codex-model-router.mjs";

const headers = { "content-type": "application/json" };
const geminiRoute = { provider: "gemini" };
const claudeRoute = { provider: "claude" };
const openaiRoute = { provider: "openai" };

function body(payload) {
  return Buffer.from(JSON.stringify(payload));
}

function decode(result) {
  return JSON.parse(result.buffer.toString("utf8"));
}

test("missing compaction capsule is incompatible with Antigravity and fails closed", () => {
  const result = sanitizeBodyForRoute(
    body({ model: "gemini-3.8-flash-high", input: [{ type: "compaction", id: "cmp_1" }] }),
    headers,
    geminiRoute,
  );

  assert.equal(result.compactionGuard.action, "blocked");
  assert.equal(result.compactionGuard.incompatible[0].format, "missing");
  assert.equal(result.changed, false);
});

test("OpenAI capsule is blocked when the target is Claude through Antigravity", () => {
  const result = sanitizeBodyForRoute(
    body({
      model: "claude-sonnet-4-6",
      input: [{ type: "compaction", encrypted_content: "gAAAAABforeign" }],
    }),
    headers,
    claudeRoute,
  );

  assert.equal(result.compactionGuard.action, "blocked");
  assert.equal(result.compactionGuard.incompatible[0].format, "openai");
});

test("provider switch exposes explicit handoff and cancel choices", () => {
  assert.deepEqual(
    PROVIDER_SWITCH_CHOICES.map(({ id }) => id),
    ["handoff", "cancel"],
  );
  assert.equal(PROVIDER_SWITCH_CHOICES[0].recommended, true);
  assert.equal(PROVIDER_SWITCH_CHOICES[1].recommended, false);
  assert.equal(PROVIDER_SWITCH_CHOICES[0].label_zh, "新建目标模型任务并迁移摘要");
  assert.equal(PROVIDER_SWITCH_CHOICES[1].label_zh, "取消切换并继续原模型任务");
});

test("valid Antigravity capsule is preserved", () => {
  const payload = {
    model: "gemini-3.8-flash-high",
    input: [{ type: "compaction", encrypted_content: "cpa-ag-compact-v1:sealed" }],
  };
  const result = sanitizeBodyForRoute(body(payload), headers, geminiRoute);

  assert.equal(result.compactionGuard, null);
  assert.deepEqual(decode(result), payload);
});

test("drop_foreign removes invalid compaction and previous response linkage", () => {
  const result = sanitizeBodyForRoute(
    body({
      model: "gemini-3.8-flash-high",
      previous_response_id: "resp_foreign",
      input: [
        { type: "message", role: "user", content: "ping" },
        { type: "compaction", encrypted_content: "gAAAAABforeign" },
      ],
    }),
    headers,
    geminiRoute,
    { providerSwitchCompactionMode: "drop_foreign" },
  );

  const sanitized = decode(result);
  assert.equal(result.compactionGuard.action, "dropped");
  assert.equal(result.compactionDropped, 1);
  assert.equal("previous_response_id" in sanitized, false);
  assert.deepEqual(sanitized.input, [{ type: "message", role: "user", content: "ping" }]);
});

test("foreign Gemini carrier is blocked when targeting OpenAI", () => {
  const inspection = inspectCompactionForRoute(
    { input: [{ type: "compaction", encrypted_content: "cpa-ag-compact-v1:sealed" }] },
    openaiRoute,
  );
  assert.equal(inspection.compatible, false);
  assert.equal(inspection.incompatible[0].capsule.family, "antigravity");
});

test("identity rewrite remains limited to Antigravity routes", () => {
  const payload = {
    model: "gemini-3.8-flash-high",
    input: [{ type: "message", content: "You are Codex, an agent based on GPT-5." }],
  };
  const result = sanitizeBodyForRoute(body(payload), headers, geminiRoute);
  assert.equal(result.promptRewriteCount, 1);
  assert.equal(decode(result).input[0].content, "You are a helpful AI coding assistant.");
});

test("compaction mode rejects unknown values", () => {
  assert.throws(
    () => createConfig({ CODEX_BRIDGE_PROVIDER_SWITCH_COMPACTION_MODE: "unsafe" }),
    /Invalid provider switch compaction mode/,
  );
});

test("same-provider Responses state is preserved", () => {
  const payload = {
    model: "gpt-5.6-sol",
    previous_response_id: "resp_same_provider",
    input: [{ type: "reasoning", id: "rs_resp_same_provider" }],
  };
  const inspection = inspectProviderSwitchState(payload, openaiRoute, "openai");
  assert.equal(inspection.compatible, true);
  const result = sanitizeBodyForRoute(body(payload), headers, openaiRoute, {
    previousProvider: "openai",
  });
  assert.equal(result.providerSwitchGuard, null);
  assert.deepEqual(decode(result), payload);
});

test("cross-provider Responses state fails closed before upstream", () => {
  const payload = {
    model: "gpt-5.6-sol",
    store: false,
    previous_response_id: "resp_gemini_state",
    input: [
      { type: "message", role: "user", content: "continue" },
      { type: "reasoning", id: "rs_resp_8yuyagfzBM6X9tMPv8KXgQ0_0" },
      { type: "item_reference", item_id: "msg_gemini_1" },
    ],
  };
  const result = sanitizeBodyForRoute(body(payload), headers, openaiRoute, {
    previousProvider: "gemini",
    providerSwitchStateMode: "fail_closed",
  });
  assert.equal(result.providerSwitchGuard.action, "blocked");
  assert.equal(result.providerSwitchGuard.sourceProvider, "gemini");
  assert.equal(result.providerSwitchGuard.targetProvider, "openai");
  assert.match(result.providerSwitchGuard.reasons.join(" "), /rs_resp_/);
  assert.equal(result.changed, false);
});

test("explicit drop_foreign removes provider state but preserves ordinary messages", () => {
  const payload = {
    model: "gemini-3.8-flash-high",
    store: false,
    previous_response_id: "resp_openai_state",
    input: [
      { type: "message", role: "user", content: "continue" },
      { type: "reasoning", id: "rs_resp_openai_1", summary: [] },
      { type: "function_call", id: "fc_openai_1", name: "shell" },
      { type: "item_reference", item_id: "msg_openai_1" },
    ],
  };
  const result = sanitizeBodyForRoute(body(payload), headers, geminiRoute, {
    previousProvider: "openai",
    providerSwitchStateMode: "drop_foreign",
  });
  const sanitized = decode(result);
  assert.equal(result.providerSwitchGuard.action, "dropped");
  assert.equal(result.providerStateDropped, 4);
  assert.equal("previous_response_id" in sanitized, false);
  assert.deepEqual(sanitized.input, [{ type: "message", role: "user", content: "continue" }]);
  assert.equal(removeProviderScopedResponseState(payload).dropped >= 4, true);
});

test("provider switch state mode rejects unknown values", () => {
  assert.throws(
    () => createConfig({ CODEX_BRIDGE_PROVIDER_SWITCH_STATE_MODE: "unsafe" }),
    /Invalid provider switch state mode/,
  );
  assert.equal(
    createConfig({ CODEX_BRIDGE_PROVIDER_SWITCH_STATE_MODE: "drop_foreign" }).providerSwitchStateMode,
    "drop_foreign",
  );
});

const poolError = JSON.stringify({
  error: {
    code: "internal_server_error",
    message: 'auth_unavailable: no auth available (providers=antigravity, model=gemini-3.8-flash-high; last upstream error: {"reason":"VALIDATION_REQUIRED","validation_url":"private-flow-token"})',
  },
});

function makeBlockedPool(t, now = Date.now(), model = "gemini-3.8-flash-high") {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-pool-test-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  for (const [name, httpStatus, reason] of [
    ["one", 403, "VALIDATION_REQUIRED"],
    ["two", 403, "VALIDATION_REQUIRED"],
    ["three", 429, "quota"],
  ]) {
    const authId = `${name}.json`;
    fs.writeFileSync(path.join(dir, authId), JSON.stringify({ type: "antigravity", disabled: false }));
    fs.writeFileSync(path.join(dir, `${name}.cds`), JSON.stringify({
      provider: "antigravity",
      auth_id: authId,
      records: [{
        model,
        status: "cooling",
        next_retry_after: new Date(now + 30 * 60 * 1000).toISOString(),
        reason,
        last_error: {
          http_status: httpStatus,
          message: reason === "quota" ? "quota exhausted" :
            '{"reason":"VALIDATION_REQUIRED","validation_url":"private-flow-token"}',
        },
      }],
    }));
  }
  return dir;
}

test("classifies only a fully cooling Antigravity pool and never returns private URLs", (t) => {
  const now = Date.now();
  const dir = makeBlockedPool(t, now);
  const result = classifyAntigravityAuthUnavailable(
    503, Buffer.from(poolError), dir, "gemini-3.8-flash-high", now,
  );
  assert.equal(result.enabled, 3);
  assert.equal(result.validation, 2);
  assert.equal(result.quota, 1);
  assert.equal(JSON.stringify(result).includes("private-flow-token"), false);
  assert.equal(classifyAntigravityAuthUnavailable(
    503, Buffer.from(poolError), dir, "gemini-3.8-flash-high", now + 31 * 60 * 1000,
  ), null);
  assert.equal(classifyAntigravityAuthUnavailable(
    503, Buffer.from('{"error":{"message":"No capacity available"}}'), dir,
    "gemini-3.8-flash-high", now,
  ), null);
  assert.equal(classifyAntigravityAuthUnavailable(
    503, Buffer.from(poolError), dir, "gpt-6-luna", now,
  ), null);
});

test("one healthy or unknown credential keeps the original upstream failure", (t) => {
  const now = Date.now();
  const dir = makeBlockedPool(t, now);
  fs.writeFileSync(path.join(dir, "healthy.json"), JSON.stringify({ type: "antigravity" }));
  assert.equal(classifyAntigravityAuthUnavailable(
    503, Buffer.from(poolError), dir, "gemini-3.8-flash-high", now,
  ), null);
});

test("Gemini pool exhaustion is one sanitized, non-retryable handoff error", async (t) => {
  const dir = makeBlockedPool(t);
  const helper = path.join(dir, "client-key.py");
  fs.writeFileSync(helper, 'process.stdout.write("dummy-local-key");\n');
  let attempts = 0;
  const server = createRouterServer({
    config: { authDir: dir, helper, helperPython: process.execPath },
    log: () => {},
    requestImpl: (_target, _options, callback) => {
      const request = new EventEmitter();
      request.setTimeout = () => {};
      request.end = (sentBody) => {
        attempts += 1;
        const input = JSON.parse(sentBody.toString("utf8"));
        const reply = input.input === "capacity"
          ? JSON.stringify({ error: { message: "No capacity available for model" } })
          : poolError;
        const upstream = Readable.from([Buffer.from(reply)]);
        upstream.statusCode = 503;
        upstream.headers = { "content-type": "application/json", "content-length": String(Buffer.byteLength(reply)) };
        queueMicrotask(() => callback(upstream));
      };
      return request;
    },
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const response = await fetch(`http://127.0.0.1:${server.address().port}/v1/responses`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ model: "gemini-3.8-flash-high", input: "ping" }),
  });
  const text = await response.text();
  const payload = JSON.parse(text);
  assert.equal(response.status, 400);
  assert.equal(attempts, 1);
  assert.equal(payload.error.code, "antigravity_account_action_required");
  assert.equal(payload.retryable, false);
  assert.equal(payload.handoff_mode, "new_task_with_plain_text_summary");
  assert.equal(text.includes("private-flow-token"), false);
  assert.equal(text.includes("@gmail.com"), false);
  const capacity = await fetch(`http://127.0.0.1:${server.address().port}/v1/responses`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ model: "gemini-3.8-flash-high", input: "capacity" }),
  });
  assert.equal(capacity.status, 503);
  assert.match(await capacity.text(), /No capacity available/);
  const native = await fetch(`http://127.0.0.1:${server.address().port}/v1/responses`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ model: "gpt-6-luna", input: "ping" }),
  });
  assert.equal(native.status, 503);
  assert.equal(attempts, 3);
});

test("isExternalMode detects explicit bridge mode and ignores casing", () => {
  assert.equal(isExternalMode({ headers: { "x-codex-bridge-mode": "external" } }), true);
  assert.equal(isExternalMode({ headers: { "x-codex-bridge-mode": "EXTERNAL" } }), true);
  assert.equal(isExternalMode({ "x-codex-bridge-mode": "External" }), true);
  assert.equal(isExternalMode({ headers: { "x-codex-bridge-mode": "normal" } }), false);
  assert.equal(isExternalMode({}), false);
  assert.equal(isExternalMode(null), false);
});

test("stripInternalHeaders strips internal bridge headers", () => {
  const stripped = stripInternalHeaders({
    "content-type": "application/json",
    "x-codex-bridge-mode": "external",
    authorization: "Bearer token",
  });
  assert.equal("x-codex-bridge-mode" in stripped, false);
  assert.equal(stripped["content-type"], "application/json");
  assert.equal(stripped.authorization, "Bearer token");
});

test("external mode fast-fails GPT requests with 409 without contacting upstream", async (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-fastfail-test-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const helper = path.join(dir, "client-key.py");
  fs.writeFileSync(helper, 'process.stdout.write("dummy-local-key");\n');

  let upstreamCalled = false;
  const server = createRouterServer({
    config: { authDir: dir, helper, helperPython: process.execPath },
    log: () => {},
    requestImpl: () => {
      upstreamCalled = true;
      throw new Error("Upstream should never be called for external GPT request");
    },
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));

  const res = await fetch(`http://127.0.0.1:${server.address().port}/v1/responses`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-codex-bridge-mode": "external",
    },
    body: JSON.stringify({ model: "gpt-5.6-sol", input: "ping" }),
  });

  assert.equal(res.status, 409);
  assert.equal(upstreamCalled, false);
  const data = await res.json();
  assert.equal(data.error, "gpt_unavailable_in_external_mode");
  assert.equal(data.retryable, false);
  assert.match(data.message, /外部模型模式/);
});

test("external mode dynamically enables drop_foreign and strips internal header upstream", async (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-ext-switch-test-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const helper = path.join(dir, "client-key.py");
  fs.writeFileSync(helper, 'process.stdout.write("dummy-local-key");\n');

  let lastUpstreamHeaders = null;
  let lastUpstreamBody = null;

  const server = createRouterServer({
    config: { authDir: dir, helper, helperPython: process.execPath },
    log: () => {},
    requestImpl: (_target, options, callback) => {
      lastUpstreamHeaders = options.headers;
      const req = new EventEmitter();
      req.setTimeout = () => {};
      req.end = (sentBody) => {
        lastUpstreamBody = JSON.parse(sentBody.toString("utf8"));
        const reply = JSON.stringify({ id: "resp_ok", choices: [] });
        const upstream = Readable.from([Buffer.from(reply)]);
        upstream.statusCode = 200;
        upstream.headers = { "content-type": "application/json" };
        queueMicrotask(() => callback(upstream));
      };
      return req;
    },
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));

  const threadId = "thread_ext_test_123";

  // Request 1: Gemini in external mode
  const res1 = await fetch(`http://127.0.0.1:${server.address().port}/v1/responses`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-codex-bridge-mode": "external",
      "x-codex-thread-id": threadId,
    },
    body: JSON.stringify({
      model: "gemini-3.8-flash-high",
      input: [{ type: "message", role: "user", content: "hello from gemini" }],
    }),
  });
  assert.equal(res1.status, 200);
  assert.equal("x-codex-bridge-mode" in lastUpstreamHeaders, false);

  // Request 2: Claude with foreign Gemini state in same thread with external mode
  const res2 = await fetch(`http://127.0.0.1:${server.address().port}/v1/responses`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-codex-bridge-mode": "external",
      "x-codex-thread-id": threadId,
    },
    body: JSON.stringify({
      model: "claude-sonnet-4-6",
      previous_response_id: "resp_gemini_foreign",
      input: [
        { type: "reasoning", id: "rs_resp_gemini_1" },
        { type: "item_reference", item_id: "msg_gemini_1" },
        { type: "message", role: "user", content: "continue with claude" },
      ],
    }),
  });
  assert.equal(res2.status, 200);
  assert.equal("x-codex-bridge-mode" in lastUpstreamHeaders, false);
  // Foreign provider state was dropped, ordinary message kept:
  assert.equal("previous_response_id" in lastUpstreamBody, false);
  assert.deepEqual(lastUpstreamBody.input, [
    { type: "message", role: "user", content: "continue with claude" },
  ]);

  // Request 3: Switch to DeepSeek with external mode
  const res3 = await fetch(`http://127.0.0.1:${server.address().port}/v1/responses`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-codex-bridge-mode": "external",
      "x-codex-thread-id": threadId,
    },
    body: JSON.stringify({
      model: "deepseek-v4-flash",
      previous_response_id: "resp_claude_foreign",
      input: [
        { type: "reasoning", id: "rs_resp_claude_1" },
        { type: "message", role: "user", content: "continue with deepseek" },
      ],
    }),
  });
  assert.equal(res3.status, 200);
  assert.equal("previous_response_id" in lastUpstreamBody, false);
  assert.deepEqual(lastUpstreamBody.input, [
    { type: "message", role: "user", content: "continue with deepseek" },
  ]);
});

test("resolveRoute correctly identifies thirdparty bridge models", () => {
  for (const model of ["grok-4.6", "grok-beta", "zai-general", "custom-my-model", "qwen-max"]) {
    const route = resolveRoute(model);
    assert.ok(route, `route should exist for ${model}`);
    assert.equal(route.id, "thirdparty-bridge");
    assert.equal(route.provider, "thirdparty");
    assert.equal(route.transport, "bridge");
    assert.equal(route.upstream, "cliProxy");
  }
});

test("classifyAntigravityAuthUnavailable handles Claude models correctly", (t) => {
  const now = Date.now();
  const model = "claude-sonnet-4-6";
  const dir = makeBlockedPool(t, now, model);
  const claudePoolError = JSON.stringify({
    error: {
      code: "internal_server_error",
      message: `auth_unavailable: no auth available (providers=antigravity, model=${model}; last upstream error: {"reason":"VALIDATION_REQUIRED","validation_url":"private-flow-token"})`,
    },
  });
  const result = classifyAntigravityAuthUnavailable(
    503, Buffer.from(claudePoolError), dir, model, now,
  );
  assert.ok(result);
  assert.equal(result.enabled, 3);
  assert.equal(result.validation, 2);
  assert.equal(result.quota, 1);
});

test("SlidingWindow handles empty, single, multi, and ring buffer wrap-around", () => {
  const win = new SlidingWindow(4);
  assert.equal(win.size(), 0);
  assert.deepEqual(win.summary(), { count: 0, sample_size: 0, min: 0, max: 0, avg: 0, p50: 0, p95: 0 });

  win.push(10);
  assert.equal(win.size(), 1);
  assert.deepEqual(win.summary(), { count: 1, sample_size: 1, min: 10, max: 10, avg: 10, p50: 10, p95: 10 });

  win.push(20);
  win.push(30);
  win.push(40);
  assert.equal(win.size(), 4);
  const s4 = win.summary();
  assert.equal(s4.count, 4);
  assert.equal(s4.sample_size, 4);
  assert.equal(s4.min, 10);
  assert.equal(s4.max, 40);
  assert.equal(s4.avg, 25);
  assert.equal(s4.p50, 25);

  // Wrap around: push 50 (overwrites 10)
  win.push(50);
  assert.equal(win.size(), 4);
  const s5 = win.summary();
  assert.equal(s5.count, 5);
  assert.equal(s5.sample_size, 4);
  assert.equal(s5.min, 20);
  assert.equal(s5.max, 50);
  assert.equal(s5.avg, 35);

  win.reset();
  assert.equal(win.size(), 0);
  assert.equal(win.summary().count, 0);
});

test("MetricsCollector accurately aggregates socket reuse and latencies by upstream", () => {
  const collector = new MetricsCollector(100);
  const t1 = collector.startRequest();
  assert.equal(collector.activeRequests, 1);
  assert.equal(collector.totalRequests, 0);

  collector.recordRequest({
    upstream: "gptNative",
    reusedSocket: true,
    ingressMs: 1.2,
    sanitizeMs: 0.5,
    queueMs: 0.1,
    dnsMs: 0,
    connectMs: 0,
    tlsMs: 0,
    handshakeMs: 0,
    ttfbMs: 150.0,
    ttftMs: 200.0,
    totalMs: 500.0,
  });
  assert.equal(collector.activeRequests, 0);

  collector.recordRequest({
    upstream: "cliProxy",
    reusedSocket: false,
    ingressMs: 0.8,
    sanitizeMs: 0.4,
    queueMs: 0.2,
    dnsMs: 10.0,
    connectMs: 20.0,
    tlsMs: 30.0,
    handshakeMs: 60.0,
    ttfbMs: 80.0,
    ttftMs: 110.0,
    totalMs: 300.0,
  });

  const snap = collector.snapshot();
  assert.equal(snap.total_requests, 2);
  assert.equal(snap.active_requests, 0);
  assert.equal(snap.socket_reuse.reused_count, 1);
  assert.equal(snap.socket_reuse.new_count, 1);
  assert.equal(snap.socket_reuse.reuse_rate_pct, 50.0);

  assert.equal(snap.latencies.ttfb_ms.count, 2);
  assert.equal(snap.latencies.ttfb_ms.min, 80.0);
  assert.equal(snap.latencies.ttfb_ms.max, 150.0);

  assert.equal(snap.by_upstream.gptNative.total_requests, 1);
  assert.equal(snap.by_upstream.gptNative.reused_sockets, 1);
  assert.equal(snap.by_upstream.gptNative.reuse_rate_pct, 100.0);

  assert.equal(snap.by_upstream.cliProxy.total_requests, 1);
  assert.equal(snap.by_upstream.cliProxy.new_sockets, 1);
  assert.equal(snap.by_upstream.cliProxy.reuse_rate_pct, 0.0);
});

test("router exposes /__codex_bridge_metrics and supports ?reset=1", async (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-metrics-test-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const helper = path.join(dir, "client-key.py");
  fs.writeFileSync(helper, 'process.stdout.write("dummy-local-key");\n');

  const server = createRouterServer({
    config: { authDir: dir, helper, helperPython: process.execPath },
    log: () => {},
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));

  const base = `http://127.0.0.1:${server.address().port}`;

  const res1 = await fetch(`${base}/__codex_bridge_metrics`);
  assert.equal(res1.status, 200);
  const snap1 = await res1.json();
  assert.equal(snap1.total_requests, 0);
  assert.equal(snap1.socket_reuse.reuse_rate_pct, 0);

  server.metricsCollector.recordRequest({
    upstream: "gptNative",
    reusedSocket: true,
    totalMs: 123,
  });

  const res2 = await fetch(`${base}/__codex_bridge_metrics`);
  const snap2 = await res2.json();
  assert.equal(snap2.total_requests, 1);
  assert.equal(snap2.socket_reuse.reused_count, 1);
  assert.equal(snap2.socket_reuse.reuse_rate_pct, 100.0);

  const resReset = await fetch(`${base}/__codex_bridge_metrics?reset=1`);
  assert.equal(resReset.status, 200);
  const resetBody = await resReset.json();
  assert.equal(resetBody.status, "reset");

  const res3 = await fetch(`${base}/__codex_bridge_metrics`);
  const snap3 = await res3.json();
  assert.equal(snap3.total_requests, 0);
});

test("router measures full-chain socket lifecycle, TTFB, TTFT, and emits request_perf logs", async (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-perf-test-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const helper = path.join(dir, "client-key.py");
  fs.writeFileSync(helper, 'process.stdout.write("dummy-local-key");\n');

  const logs = [];
  const server = createRouterServer({
    config: { authDir: dir, helper, helperPython: process.execPath },
    log: (line) => logs.push(line),
    requestImpl: (_target, _options, callback) => {
      const req = new EventEmitter();
      req.reusedSocket = true;
      req.setTimeout = () => {};
      req.end = () => {
        const stream = new Readable({
          read() {},
        });
        stream.statusCode = 200;
        stream.headers = { "content-type": "text/event-stream" };
        callback(stream);

        setTimeout(() => {
          stream.push(Buffer.from('data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'));
        }, 10);

        setTimeout(() => {
          stream.push(Buffer.from('data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'));
        }, 30);

        setTimeout(() => {
          stream.push(Buffer.from('data: [DONE]\n\n'));
          stream.push(null);
        }, 50);
      };
      return req;
    },
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));

  const base = `http://127.0.0.1:${server.address().port}`;
  const res = await fetch(`${base}/v1/responses`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ model: "gpt-5.6-sol", input: "ping" }),
  });

  assert.equal(res.status, 200);
  const text = await res.text();
  assert.match(text, /Hello/);

  const snap = server.metricsCollector.snapshot();
  assert.equal(snap.total_requests, 1);
  assert.equal(snap.socket_reuse.reused_count, 1);
  assert.equal(snap.socket_reuse.reuse_rate_pct, 100.0);
  assert.ok(snap.latencies.ttfb_ms.count >= 1);
  assert.ok(snap.latencies.ttft_ms.count >= 1);
  assert.ok(snap.latencies.total_ms.avg > 0);

  const perfLog = logs.find((l) => l.includes("event=request_perf"));
  assert.ok(perfLog, "request_perf log should be emitted");
  assert.match(perfLog, /reused=true/);
  assert.match(perfLog, /ttfb_ms=/);
  assert.match(perfLog, /ttft_ms=/);
  assert.match(perfLog, /total_ms=/);
  assert.match(perfLog, /status=200/);
});



